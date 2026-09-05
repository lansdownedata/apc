"""APC-26 — authorize at checkout, capture only once APC confirms the order.

The spine change: a paid quote is no longer a booked order. The customer's card is
authorized (money on hold, nothing moved), the lead sits in ENGAGED, and a human at APC
decides Confirm (capture + book) or Cancel (release the hold).
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.leads.models import Lead
from apps.payments import services, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _authorized_intent(pi="pi_1", amount=133500):
    return MagicMock(
        id=pi,
        status="requires_capture",
        amount=amount,
        amount_capturable=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _captured_intent(pi="pi_1", amount=133500):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _authorizable_plan(**kw):
    kw.setdefault("quote_total", Decimal("2670.00"))
    plan = PaymentPlanFactory(**kw)
    plan.lead.status = Lead.Status.QUOTED
    plan.lead.save(update_fields=["status"])
    return plan


def _authorize(plan, *, pi="pi_1"):
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = pi
    charge.save(update_fields=["stripe_payment_intent_id"])
    with patch.object(
        services.stripe.PaymentIntent, "retrieve", return_value=_authorized_intent(pi)
    ):
        services.record_authorization(plan, pi)
    return charge


# --- checkout authorizes, it does not charge -----------------------------------------


def test_deposit_intent_is_created_for_manual_capture():
    """The one flag that makes checkout hold instead of take."""
    plan = _authorizable_plan()
    with (
        patch.object(services, "get_or_create_customer", return_value="cus_1"),
        patch.object(services.stripe.PaymentIntent, "create") as create,
    ):
        create.return_value = MagicMock(id="pi_1", client_secret="cs_1")
        services.open_intent_for(plan, kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)

    assert create.call_args.kwargs["capture_method"] == "manual"


def test_a_balance_charge_still_captures_automatically():
    """Only the deposit waits on a human — a balance charge is owed money, not a hold."""
    plan = _authorizable_plan()
    with (
        patch.object(services, "get_or_create_customer", return_value="cus_1"),
        patch.object(services.stripe.PaymentIntent, "create") as create,
    ):
        create.return_value = MagicMock(id="pi_2", client_secret="cs_2")
        services.open_intent_for(plan, kind=Charge.Kind.BALANCE, amount=Decimal("100.00"))

    assert create.call_args.kwargs.get("capture_method") != "manual"


# --- recording the authorization -------------------------------------------------


def test_authorization_engages_the_lead_without_moving_money():
    plan = _authorizable_plan()

    charge = _authorize(plan)

    plan.refresh_from_db()
    charge.refresh_from_db()
    plan.lead.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED
    assert charge.authorized_at is not None
    assert plan.deposit_status == PaymentPlan.DepositStatus.AUTHORIZED
    assert plan.lead.status == Lead.Status.ENGAGED
    # nothing captured: no ledger entry, no money recognised
    assert not JournalEntry.objects.filter(lead=plan.lead).exists()


def test_authorization_stores_the_card_for_later_capture():
    plan = _authorizable_plan()
    _authorize(plan)
    plan.refresh_from_db()
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_last4 == "4242"


def test_authorization_sets_a_capture_deadline():
    plan = _authorizable_plan()
    charge = _authorize(plan)
    charge.refresh_from_db()
    expected = charge.authorized_at + timedelta(days=services.AUTH_HOLD_DAYS)
    assert abs((charge.capture_expires_at - expected).total_seconds()) < 2


def test_recording_the_same_authorization_twice_is_a_no_op():
    """The inline `complete` POST and the webhook both reconcile the same intent."""
    plan = _authorizable_plan()
    charge = _authorize(plan)
    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_authorized_intent()):
        services.record_authorization(plan, "pi_1")

    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT).count() == 1
    charge.refresh_from_db()
    assert charge.status == Charge.Status.AUTHORIZED


# --- the webhook that changes meaning under manual capture ---------------------------


def _capturable_event(lead_id, *, pi="pi_1"):
    return {
        "type": "payment_intent.amount_capturable_updated",
        "data": {"object": {"id": pi, "metadata": {"lead_id": str(lead_id), "kind": "deposit"}}},
    }


def test_amount_capturable_updated_records_the_authorization():
    """Under manual capture `payment_intent.succeeded` no longer fires at checkout — this
    event is the only signal that the customer paid, so missing it loses the order."""
    plan = _authorizable_plan()
    with patch.object(
        webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_authorized_intent()
    ):
        webhooks.process_stripe_event(_capturable_event(plan.lead_id))

    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.AUTHORIZED
    assert plan.lead.status == Lead.Status.ENGAGED


# --- confirm ------------------------------------------------------------------------


def test_confirm_captures_and_books():
    plan = _authorizable_plan()
    _authorize(plan)

    with (
        patch.object(services.stripe.PaymentIntent, "capture") as capture,
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_captured_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.confirm_order(plan.lead, user=None)

    capture.assert_called_once()
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED).exists()
    assert JournalEntry.objects.filter(
        lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED
    ).exists()


def test_confirm_refuses_an_order_that_was_never_authorized():
    plan = _authorizable_plan()
    with pytest.raises(services.PaymentError):
        services.confirm_order(plan.lead, user=None)


def test_confirming_twice_is_idempotent():
    plan = _authorizable_plan()
    _authorize(plan)
    with (
        patch.object(services.stripe.PaymentIntent, "capture"),
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_captured_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.confirm_order(plan.lead, user=None)
        services.confirm_order(plan.lead, user=None)

    assert (
        JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED).count()
        == 1
    )


# --- cancel -------------------------------------------------------------------------


def test_cancel_releases_the_hold_and_loses_the_lead():
    plan = _authorizable_plan()
    charge = _authorize(plan)

    with patch.object(services.stripe.PaymentIntent, "cancel") as cancel:
        services.cancel_order(plan.lead, user=None, reason="No coach available")

    cancel.assert_called_once()
    charge.refresh_from_db()
    plan.lead.refresh_from_db()
    assert charge.status == Charge.Status.RELEASED
    assert plan.lead.status == Lead.Status.LOST
    assert plan.lead.lost_reason == "No coach available"
    # the hold never became money
    assert not JournalEntry.objects.filter(lead=plan.lead).exists()


def test_cancel_refuses_an_order_that_was_never_authorized():
    plan = _authorizable_plan()
    with pytest.raises(services.PaymentError):
        services.cancel_order(plan.lead, user=None, reason="x")


# --- the engaged state must not behave like a live quote ------------------------------


def test_an_engaged_quote_does_not_expire():
    """They have paid and are waiting on us — the quote must not lapse underneath them."""
    plan = _authorizable_plan()
    _authorize(plan)
    lead = plan.lead
    lead.refresh_from_db()
    lead.quote_expires_at = timezone.now() - timedelta(days=1)
    lead.save(update_fields=["quote_expires_at"])

    assert lead.quote_expired is False


def test_engaged_is_a_legal_transition_from_quoted_and_leads_to_booked_or_lost():
    lead = _authorizable_plan().lead
    assert lead.can_transition(Lead.Status.ENGAGED)
    lead.status = Lead.Status.ENGAGED
    assert lead.can_transition(Lead.Status.BOOKED)
    assert lead.can_transition(Lead.Status.LOST)
