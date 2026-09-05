"""The public pay page — deposit or balance, whichever is owed, collected on our own page.

Replaces the hosted-Checkout `quote_book` redirect (spec 2026-08-30 §8). The signed quote
token is the auth; what is owed is resolved from the ledger, never the plan flags alone.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads import services
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _lead(**kwargs):
    kwargs.setdefault("status", Lead.Status.QUOTED)
    if "contact" not in kwargs:
        kwargs["contact"] = ContactFactory()
    kwargs.setdefault("quote_expires_at", timezone.now() + timezone.timedelta(days=10))
    total = kwargs.pop("total", Decimal("1000.00"))
    lead = LeadFactory(**kwargs)
    TransferReservationFactory(
        lead=lead, rate=total, service_type=ServiceTypeFactory(name="Airport Transfer")
    )
    PaymentPlanFactory(lead=lead, quote_total=total, deposit_pct=50)
    return lead


def _tok(lead):
    return services.make_deposit_token(lead)


def _collect(lead, amount, kind=JournalEntry.Kind.DEPOSIT_CAPTURED):
    ledger.post_capture(
        lead=lead, amount=Decimal(amount), kind=kind, idempotency_key=f"seed-{lead.pk}-{amount}"
    )


def _intent(pi="pi_1", status="succeeded", amount=50000):
    return MagicMock(
        id=pi,
        status=status,
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


# --- quote_pay renders the right state ------------------------------------
def test_pay_page_renders_card_form_for_an_unpaid_deposit(client):
    lead = _lead()
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "apcPay.mount(" in body
    assert "500.00" in body  # the 50% deposit
    assert resp.context["pay_kind"] == Charge.Kind.DEPOSIT


def test_pay_page_renders_card_form_for_an_outstanding_balance(client):
    lead = _lead()
    _collect(lead, "500.00")  # deposit paid
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    assert resp.context["pay_kind"] == Charge.Kind.BALANCE
    assert resp.context["amount"] == Decimal("500.00")


def test_pay_page_shows_no_form_when_nothing_is_owed(client):
    lead = _lead()
    _collect(lead, "1000.00")
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "apcPay.mount(" not in body
    assert resp.context["pay_kind"] is None


def test_pay_page_shows_no_form_when_the_quote_expired(client):
    lead = _lead(quote_expires_at=timezone.now() - timezone.timedelta(days=1))
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    assert "apcPay.mount(" not in resp.content.decode()


def test_pay_page_shows_no_form_when_the_lead_is_lost(client):
    lead = _lead()
    lead.status = Lead.Status.LOST
    lead.save(update_fields=["status"])
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    assert "apcPay.mount(" not in resp.content.decode()


def test_pay_page_owed_is_computed_from_the_ledger_after_a_partial_staff_charge(client):
    lead = _lead(total=Decimal("2000.00"))  # deposit = 1000
    _collect(lead, "300.00")  # a partial staff charge
    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.context["pay_kind"] == Charge.Kind.DEPOSIT
    assert resp.context["amount"] == Decimal("700.00")  # 1000 deposit − 300 collected


def test_pay_page_bad_token_404s(client):
    assert client.get(reverse("quote_pay", args=["nope"])).status_code == 404


# --- quote_pay_intent -----------------------------------------------------
def test_intent_returns_a_client_secret(client):
    lead = _lead()
    with patch(
        "apps.leads.views.payment_services.open_intent_for",
        return_value=(MagicMock(pk=1), "pi_1_secret"),
    ):
        resp = client.post(reverse("quote_pay_intent", args=[_tok(lead)]))
    assert resp.status_code == 200
    assert resp.json()["client_secret"] == "pi_1_secret"


def test_intent_reuses_the_same_intent_on_a_second_call(client):
    lead = _lead()
    with patch.object(services_stripe(), "PaymentIntent") as pi:
        pi.create.side_effect = [
            MagicMock(id="pi_1", client_secret="pi_1_secret"),
            MagicMock(id="pi_2", client_secret="pi_2_secret"),
        ]
        url = reverse("quote_pay_intent", args=[_tok(lead)])
        first = client.post(url).json()
        second = client.post(url).json()
    assert first["client_secret"] == second["client_secret"]
    assert lead.payment.charges.count() == 1


def test_intent_refuses_on_lost(client):
    lead = _lead()
    lead.status = Lead.Status.LOST
    lead.save(update_fields=["status"])
    resp = client.post(reverse("quote_pay_intent", args=[_tok(lead)]))
    assert resp.status_code == 400


def test_intent_refuses_when_nothing_owed(client):
    lead = _lead()
    _collect(lead, "1000.00")
    resp = client.post(reverse("quote_pay_intent", args=[_tok(lead)]))
    assert resp.status_code == 400


def test_intent_get_not_allowed(client):
    lead = _lead()
    assert client.get(reverse("quote_pay_intent", args=[_tok(lead)])).status_code == 405


# --- quote_pay_complete -------------------------------------------------------
def test_complete_engages_the_order_without_booking_it(client):
    """APC-26 reversed this: a deposit *authorizes* at checkout. The customer is done, but
    the order is not booked until APC confirms availability and captures."""
    lead = _lead()
    charge = lead.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_1"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    with (
        patch.object(
            services_stripe().PaymentIntent,
            "retrieve",
            return_value=_intent(status="requires_capture"),
        ),
        patch("apps.integrations.la_sync.push_lead_bookings") as push,
    ):
        resp = client.post(
            reverse("quote_pay_complete", args=[_tok(lead)]), {"payment_intent_id": "pi_1"}
        )
    assert resp.status_code == 200
    lead.refresh_from_db()
    assert lead.status == Lead.Status.ENGAGED
    charge.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED
    # nothing is dispatched or pushed on an unconfirmed order
    push.assert_not_called()


def test_complete_refuses_an_intent_from_another_lead(client):
    lead = _lead()
    other = _lead()
    other_charge = other.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    other_charge.stripe_payment_intent_id = "pi_other"
    other_charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    with patch.object(services_stripe().PaymentIntent, "retrieve") as retrieve:
        resp = client.post(
            reverse("quote_pay_complete", args=[_tok(lead)]),
            {"payment_intent_id": "pi_other"},
        )
    assert resp.status_code == 400
    retrieve.assert_not_called()


# --- quote_deposit_success reconciles the 3-D Secure return -------------------
def test_success_page_reconciles_a_3ds_return(client):
    lead = _lead()
    charge = lead.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_3ds"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    url = reverse("quote_deposit_success", args=[_tok(lead)])
    with (
        patch.object(
            services_stripe().PaymentIntent, "retrieve", return_value=_intent(pi="pi_3ds")
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        resp = client.get(url, {"payment_intent": "pi_3ds", "redirect_status": "succeeded"})
    assert resp.status_code == 200
    lead.refresh_from_db()
    assert lead.status == Lead.Status.BOOKED


def test_success_page_3ds_reconcile_is_idempotent_with_the_webhook(client):
    lead = _lead()
    charge = lead.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_3ds"
    charge.status = Charge.Status.SUCCEEDED
    charge.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    _collect(lead, "500.00")  # webhook already posted the capture
    url = reverse("quote_deposit_success", args=[_tok(lead)])
    retrieve = patch.object(
        services_stripe().PaymentIntent, "retrieve", return_value=_intent(pi="pi_3ds")
    )
    with retrieve:
        resp = client.get(url, {"payment_intent": "pi_3ds", "redirect_status": "succeeded"})
    assert resp.status_code == 200
    assert ledger.order_balances(lead)["collected"] == Decimal("500.00")


# --- the old routes are gone ------------------------------------------------
def test_quote_book_route_is_gone():
    with pytest.raises(NoReverseMatch):
        reverse("quote_book", args=["x"])


def test_quote_deposit_cancel_route_is_gone():
    with pytest.raises(NoReverseMatch):
        reverse("quote_deposit_cancel", args=["x"])


def services_stripe():
    from apps.payments import services

    return services.stripe
