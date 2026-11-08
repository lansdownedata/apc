"""APC-95 — `payment_intent.canceled` for a cleaning hold (spec 2026-10-27 §4 "The bank drops
it first"): the issuer let it go before we captured it, so it reads EXPIRED. A hold we
released or captured ourselves is never flipped, and other kinds stay unhandled.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads.models import Lead
from apps.payments import webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge

pytestmark = pytest.mark.django_db


def _hold(status=Charge.Status.AUTHORIZED, *, pi="pi_hold", kind=Charge.Kind.HOLD, **kw):
    plan = PaymentPlanFactory(lead__status=Lead.Status.BOOKED)
    return plan.charges.create(
        kind=kind,
        amount=Decimal("500.00"),
        status=status,
        stripe_payment_intent_id=pi,
        idempotency_key=f"hold-{plan.pk}-{pi}",
        authorized_at=timezone.now(),
        capture_expires_at=timezone.now() + timedelta(days=6),
        **kw,
    )


def _canceled(charge, *, kind="hold"):
    return {
        "type": "payment_intent.canceled",
        "data": {
            "object": {
                "id": charge.stripe_payment_intent_id,
                "status": "canceled",
                "metadata": {
                    "lead_id": str(charge.plan.lead_id),
                    "kind": kind,
                    "charge_id": str(charge.pk),
                },
            }
        },
    }


def test_canceled_webhook_marks_an_authorized_hold_expired():
    hold = _hold()

    webhooks.process_stripe_event(_canceled(hold))

    hold.refresh_from_db()
    assert hold.status == Charge.Status.EXPIRED


def test_canceled_webhook_leaves_a_released_hold_alone():
    """Review Focus 4: our own release fires the same event; RELEASED must stay RELEASED."""
    released = _hold(Charge.Status.RELEASED)
    captured = _hold(pi="pi_captured", captured_at=timezone.now())

    webhooks.process_stripe_event(_canceled(released))
    webhooks.process_stripe_event(_canceled(captured))

    released.refresh_from_db()
    captured.refresh_from_db()
    assert released.status == Charge.Status.RELEASED
    assert captured.status == Charge.Status.AUTHORIZED


def test_canceled_webhook_ignores_non_hold_intents():
    deposit = _hold(kind=Charge.Kind.DEPOSIT, pi="pi_deposit")

    webhooks.process_stripe_event(_canceled(deposit, kind="deposit"))

    deposit.refresh_from_db()
    assert deposit.status == Charge.Status.AUTHORIZED


def test_canceled_webhook_for_an_unknown_intent_is_a_no_op():
    hold = _hold()
    event = _canceled(hold)
    event["data"]["object"]["id"] = "pi_someone_else"

    webhooks.process_stripe_event(event)

    hold.refresh_from_db()
    assert hold.status == Charge.Status.AUTHORIZED
