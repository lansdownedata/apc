"""Admin card charge: amount required, ledger, remaining, books if Quoted."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def test_create_admin_payment_intent_rejects_zero():
    plan = _plan()
    with pytest.raises(services.PaymentError):
        services.create_admin_payment_intent(plan, Decimal("0.00"))
    with pytest.raises(services.PaymentError):
        services.create_admin_payment_intent(plan, Decimal("-1.00"))


def test_record_admin_payment_posts_ledger_and_books_quoted():
    plan = _plan()
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("400.00"))
    charge.stripe_payment_intent_id = "pi_admin"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    intent = MagicMock(
        id="pi_admin",
        status="succeeded",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )
    with (
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.record_admin_payment(plan, "pi_admin")
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("400.00")
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_last4 == "4242"
    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED


def test_record_admin_payment_books_a_new_lead_too():
    plan = _plan(lead=LeadFactory(status=Lead.Status.NEW))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("400.00"))
    charge.stripe_payment_intent_id = "pi_admin_new"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    intent = MagicMock(
        id="pi_admin_new",
        status="succeeded",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )
    with (
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.record_admin_payment(plan, "pi_admin_new")
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("400.00")
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_last4 == "4242"
    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED


def test_record_admin_payment_does_not_rebook_already_booked():
    plan = _plan(lead=LeadFactory(status=Lead.Status.BOOKED))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("400.00"))
    charge.stripe_payment_intent_id = "pi_admin2"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    intent = MagicMock(id="pi_admin2", status="succeeded", payment_method=None)
    with (
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings") as push,
    ):
        services.record_admin_payment(plan, "pi_admin2")
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    push.assert_not_called()


def test_sync_plan_marks_paid_in_full_when_collected_covers_total():
    plan = _plan(lead=LeadFactory(status=Lead.Status.BOOKED))
    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal("1000.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="full",
    )
    services.sync_plan_from_collected(plan)
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID


def test_remaining_balance_is_quote_total_minus_collected():
    plan = _plan()
    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal("250.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="part",
    )
    assert services.remaining_balance(plan.lead) == Decimal("750.00")


def test_save_payment_method_stores_card_on_plan():
    plan = _plan(stripe_customer_id="cus_1")
    card = MagicMock(brand="mastercard", last4="4444")
    pm = MagicMock(id="pm_saved", customer="cus_1", card=card)
    with patch.object(services.stripe.PaymentMethod, "retrieve", return_value=pm):
        services.save_payment_method(plan, "pm_saved")
    plan.refresh_from_db()
    assert plan.stripe_payment_method_id == "pm_saved"
    assert plan.card_brand == "mastercard"
    assert plan.card_last4 == "4444"


def test_admin_intent_endpoint_rejects_zero(client):
    plan = _plan()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(reverse("order_admin_intent", args=[plan.lead_id]), {"amount": "0.00"})
    assert resp.status_code == 400
    assert "amount" in resp.json()["error"].lower()


def test_admin_intent_endpoint_rejects_empty(client):
    plan = _plan()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(reverse("order_admin_intent", args=[plan.lead_id]), {"amount": ""})
    assert resp.status_code == 400


def test_admin_intent_requires_payment_access(client):
    plan = _plan()
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    resp = client.post(reverse("order_admin_intent", args=[plan.lead_id]), {"amount": "50.00"})
    assert resp.status_code == 403


def test_mark_paid_offline_endpoint_is_gone(client):
    plan = _plan()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(f"/portal/orders/{plan.lead_id}/mark-paid/", {"amount": "50.00"})
    assert resp.status_code == 404
