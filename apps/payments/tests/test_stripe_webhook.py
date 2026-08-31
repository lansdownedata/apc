"""Stripe webhook — `payment_intent.succeeded` is the only success entry point.

Hosted Checkout is gone, so `checkout.session.completed` is no longer handled at all
(spec 2026-08-30 §7). The handler branches on `metadata.kind` ∈ {deposit, balance} and
no-ops on anything already reconciled — the off-session charges in `services` confirm and
post their own ledger entry inline, yet still emit this event.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from apps.leads.models import Lead
from apps.payments import ledger, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _pi_event(lead_id, *, kind="deposit", pi="pi_1"):
    return {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": pi, "metadata": {"lead_id": str(lead_id), "kind": kind}}},
    }


def _intent(pi="pi_1", amount=133500):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


# --- deposit ----------------------------------------------------------------
def test_deposit_saves_card_marks_paid_and_books():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id))
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_brand == "visa"
    assert plan.card_last4 == "4242"
    assert plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED).exists()
    assert JournalEntry.objects.filter(
        lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED
    ).exists()


def test_deposit_capture_pushes_lead_to_la():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings") as push,
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id))
    push.assert_called_once()
    assert push.call_args.args[0].pk == plan.lead_id


# --- balance ----------------------------------------------------------------
def test_balance_kind_reconciles_as_a_balance_capture():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch.object(
            webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent(amount=50000)
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="balance"))
    assert JournalEntry.objects.filter(
        lead=plan.lead, kind=JournalEntry.Kind.BALANCE_CAPTURED
    ).exists()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


# --- things that must not happen --------------------------------------------
def test_checkout_session_completed_is_no_longer_handled():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"lead_id": str(plan.lead_id), "kind": "deposit"},
                "payment_intent": "pi_1",
            }
        },
    }
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(event)
    retrieve.assert_not_called()
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.UNSENT
    assert plan.charges.count() == 0


def test_an_unknown_kind_is_a_no_op():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="subscription"))
    retrieve.assert_not_called()
    assert plan.charges.count() == 0


def test_a_missing_lead_id_is_a_no_op():
    event = {
        "type": "payment_intent.succeeded",
        "data": {"object": {"id": "pi_1", "metadata": {"kind": "deposit"}}},
    }
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(event)
    retrieve.assert_not_called()


def test_an_already_succeeded_charge_is_not_reconciled_twice():
    """`charge_balance` / `charge_saved_card` confirm off-session and post their own ledger
    entry inline — their success event still arrives here and must do nothing."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_inline"
    charge.status = Charge.Status.SUCCEEDED
    charge.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    ledger.post_capture(
        lead=plan.lead,
        amount=charge.amount,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        stripe_ref="pi_inline",
        memo="Balance captured",
    )
    with patch.object(webhooks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        webhooks.process_stripe_event(_pi_event(plan.lead_id, kind="balance", pi="pi_inline"))
    retrieve.assert_not_called()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


# --- failure path (unchanged) ------------------------------------------------
def test_balance_failed_marks_failed_and_alerts():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), balance_status=PaymentPlan.BalanceStatus.SCHEDULED
    )
    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_bal",
                "metadata": {"lead_id": str(plan.lead_id), "kind": "balance"},
                "last_payment_error": {"message": "Your card was declined."},
            }
        },
    }
    webhooks.process_stripe_event(event)
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.FAILED
    assert "declined" in plan.fail_reason.lower()
    plan.lead.refresh_from_db()
    assert plan.lead.has_alert is True
    from apps.notifications.models import Notification

    assert Notification.objects.filter(
        lead=plan.lead, kind=Notification.Kind.BALANCE_FAILED
    ).exists()


# --- the view ----------------------------------------------------------------
def test_view_returns_200_on_valid_signature(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch(
            "apps.payments.views.stripe.Webhook.construct_event",
            return_value=_pi_event(plan.lead_id),
        ),
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )
    assert resp.status_code == 200


def test_la_push_crash_never_breaks_stripe_200(client):
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with (
        patch(
            "apps.payments.views.stripe.Webhook.construct_event",
            return_value=_pi_event(plan.lead_id),
        ),
        patch.object(webhooks.services.stripe.PaymentIntent, "retrieve", return_value=_intent()),
        patch(
            "apps.integrations.la_sync.push_lead_bookings",
            side_effect=RuntimeError("boom"),
        ),
    ):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )
    assert resp.status_code == 200


def test_view_returns_400_on_bad_signature(client):
    err = stripe.error.SignatureVerificationError("bad sig", "sig")
    with patch("apps.payments.views.stripe.Webhook.construct_event", side_effect=err):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="x",
        )
    assert resp.status_code == 400
