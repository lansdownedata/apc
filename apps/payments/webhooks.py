"""Stripe webhook processing — reconcile deposit + balance outcomes."""

import logging

from apps.notifications.models import Notification

from . import services
from .models import Charge, PaymentPlan

logger = logging.getLogger(__name__)


_SUCCESS_KINDS = {
    "deposit": Charge.Kind.DEPOSIT,
    "balance": Charge.Kind.BALANCE,
}


def process_stripe_event(event) -> None:
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "payment_intent.succeeded":
        _payment_succeeded(obj)
    elif etype == "payment_intent.payment_failed":
        _balance_failed(obj)


def _plan_from_metadata(obj):
    lead_id = (obj.get("metadata") or {}).get("lead_id")
    if not lead_id:
        return None
    return PaymentPlan.objects.filter(lead_id=lead_id).select_related("lead").first()


def _payment_succeeded(intent) -> None:
    """The single success entry point. Branches on `metadata.kind`; plan flags are left to
    `record_payment` → `sync_plan_from_collected`, which already covers the deposit case."""
    kind = _SUCCESS_KINDS.get((intent.get("metadata") or {}).get("kind"))
    if kind is None:
        return
    plan = _plan_from_metadata(intent)
    if plan is None:
        return

    # `charge_balance` / `charge_saved_card` confirm off-session and post their own ledger
    # entry inline, but their success event still arrives here. Also absorbs a duplicate
    # delivery, independently of the ledger's idempotency key.
    if plan.charges.filter(
        stripe_payment_intent_id=intent["id"], status=Charge.Status.SUCCEEDED
    ).exists():
        return

    try:
        services.record_payment(plan, intent["id"], kind=kind)
    except Exception:
        logger.exception("Payment reconcile failed for intent %s", intent.get("id"))


def _balance_failed(intent) -> None:
    plan = _plan_from_metadata(intent)
    if plan is None:
        return
    reason = ((intent.get("last_payment_error") or {}).get("message")) or "Balance charge failed"
    reason = reason[:255]

    plan.balance_status = PaymentPlan.BalanceStatus.FAILED
    plan.fail_reason = reason
    plan.save(update_fields=["balance_status", "fail_reason", "updated_at"])

    plan.lead.has_alert = True
    plan.lead.save(update_fields=["has_alert", "updated_at"])

    pi_id = intent.get("id")
    if pi_id:
        plan.charges.filter(stripe_payment_intent_id=pi_id).update(
            status=Charge.Status.FAILED, failure_reason=reason
        )
    Notification.notify(
        plan.lead, Notification.Kind.BALANCE_FAILED, title="Balance charge failed", detail=reason
    )
