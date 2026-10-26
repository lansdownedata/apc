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


def test_pay_page_tells_an_engaged_customer_the_card_is_only_held(client):
    """An engaged order has nothing to pay but nothing has been *paid* (APC-26).

    `_pay_state` returns no kind for ENGAGED, which used to drop the page onto the
    "paid in full" branch — telling a customer money changed hands when it never left
    their account, which is exactly how you earn a chargeback on a charge that doesn't
    exist.
    """
    lead = _lead()
    lead.status = Lead.Status.ENGAGED
    lead.save(update_fields=["status"])
    charge = lead.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = timezone.now()
    charge.capture_expires_at = timezone.now() + timezone.timedelta(days=7)
    charge.save(update_fields=["status", "authorized_at", "capture_expires_at"])

    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "apcPay.mount(" not in body
    assert "paid in full" not in body
    assert "Nothing has been charged yet" in body
    assert "500.00" in body  # the amount being held
    assert resp.context["is_engaged"] is True


def test_pay_page_calls_a_deposit_an_authorization_not_a_payment(client):
    """The deposit is a hold (APC-26) — the CTA must not promise a charge."""
    lead = _lead()
    body = client.get(reverse("quote_pay", args=[_tok(lead)])).content.decode()
    assert "Authorize $500.00" in body
    assert "Pay $500.00" not in body
    assert "Deposit due" not in body


def test_pay_page_bad_token_404s(client):
    assert client.get(reverse("quote_pay", args=["nope"])).status_code == 404


# --- quote_pay_intent -----------------------------------------------------
def test_intent_returns_a_client_secret(client):
    lead = _lead()
    with patch(
        "apps.leads.views.payment_services.open_intent_for",
        return_value=(MagicMock(pk=1), "pi_1_secret"),
    ):
        resp = client.post(reverse("quote_pay_intent", args=[_tok(lead)]), {"accept_terms": "1"})
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
        first = client.post(url, {"accept_terms": "1"}).json()
        second = client.post(url, {"accept_terms": "1"}).json()
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
    """A 3-D Secure customer never runs the inline `complete` POST, so this redirect is
    their only inline path. Since APC-26 a deposit lands here as `requires_capture`, and
    reconciling it engages the order rather than booking it."""
    lead = _lead()
    charge = lead.payment.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_3ds"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    url = reverse("quote_deposit_success", args=[_tok(lead)])
    with (
        patch.object(
            services_stripe().PaymentIntent,
            "retrieve",
            return_value=_intent(pi="pi_3ds", status="requires_capture"),
        ),
        patch("apps.integrations.la_sync.push_lead_bookings") as push,
    ):
        resp = client.get(url, {"payment_intent": "pi_3ds", "redirect_status": "succeeded"})
    assert resp.status_code == 200
    lead.refresh_from_db()
    charge.refresh_from_db()
    assert lead.status == Lead.Status.ENGAGED
    assert charge.status == Charge.Status.AUTHORIZED
    push.assert_not_called()


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


# --- APC-101: a pay link for one trip --------------------------------------------------


def _link_intent(pi, amount):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        client_secret=f"{pi}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _paid_in_full(lead):
    """What allocation reads: the succeeded Stripe charge row, plus its cash."""
    lead.payment.charges.create(
        kind=Charge.Kind.BALANCE,
        amount=Decimal("1000.00"),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id="pi_full",
        idempotency_key="bal-full",
    )
    _collect(lead, "1000.00")


def _trip_link(lead, amount="200.00"):
    from apps.payments import trip_money

    trip = lead.reservations.get()
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    trip.rate = trip.rate + Decimal(amount)  # the trip now owes `amount` more than was paid
    trip.save(update_fields=["rate"])
    owed = trip_money.money_for(trip).difference  # `amount` on a paid order, more if unpaid
    with patch.object(
        services_stripe().PaymentIntent,
        "create",
        return_value=_link_intent("pi_link", int(owed * 100)),
    ):
        link, _ = trip_money.open_trip_intent(trip, owed)
    return trip, link


def test_the_pay_page_asks_for_an_open_trip_charge_by_its_trip(client):
    lead = _lead(status=Lead.Status.BOOKED)
    _paid_in_full(lead)
    trip, link = _trip_link(lead)

    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))

    assert resp.context["pay_kind"] == Charge.Kind.TRIP
    assert resp.context["amount"] == Decimal("200.00")
    body = resp.content.decode()
    assert "Due for this trip" in body and trip.reference in body


def test_the_pay_page_ignores_a_trip_link_whose_trip_owes_something_else(client):
    lead = _lead(status=Lead.Status.BOOKED)
    _paid_in_full(lead)
    trip, _link = _trip_link(lead)
    trip.rate = trip.rate - Decimal("50")  # edited since the link went out
    trip.save(update_fields=["rate"])

    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))

    assert resp.context["pay_kind"] == Charge.Kind.BALANCE
    assert resp.context["amount"] == Decimal("150.00")


def test_a_trip_link_comes_before_the_deposit_on_a_card_less_booking(client):
    lead = _lead(status=Lead.Status.BOOKED)  # nothing collected yet
    _trip_link(lead, "1200.00")

    resp = client.get(reverse("quote_pay", args=[_tok(lead)]))

    assert resp.context["pay_kind"] == Charge.Kind.TRIP


def test_paying_a_trip_link_tags_the_charge_with_the_trip_and_posts_once(client):
    lead = _lead(status=Lead.Status.BOOKED)
    _paid_in_full(lead)
    trip, link = _trip_link(lead)
    intent = _link_intent("pi_link", 20000)

    with patch.object(services_stripe().PaymentIntent, "create", return_value=intent):
        secret = client.post(reverse("quote_pay_intent", args=[_tok(lead)])).json()
    with patch.object(services_stripe().PaymentIntent, "retrieve", return_value=intent):
        done = client.post(
            reverse("quote_pay_complete", args=[_tok(lead)]), {"payment_intent_id": "pi_link"}
        )

    assert secret["client_secret"] == "pi_link_secret"
    assert done.status_code == 200
    link.refresh_from_db()
    assert link.status == Charge.Status.SUCCEEDED and link.reservation == trip
    assert JournalEntry.objects.filter(charge=link).count() == 1
    assert ledger.order_balances(lead)["collected"] == Decimal("1200.00")
