"""Stripe webhook processing — reconcile deposit + balance outcomes."""

import stripe
from django.conf import settings

from apps.leads.models import Lead
from apps.notifications.models import Notification

from . import ledger
from .models import Charge, JournalEntry, PaymentPlan


def _stripe():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def process_stripe_event(event) -> None:
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        _deposit_completed(obj)
    elif etype == "payment_intent.payment_failed":
        _balance_failed(obj)


def _plan_from_metadata(obj):
    lead_id = (obj.get("metadata") or {}).get("lead_id")
    if not lead_id:
        return None
    return PaymentPlan.objects.filter(lead_id=lead_id).select_related("lead").first()


def _deposit_completed(session) -> None:
    plan = _plan_from_metadata(session)
    if plan is None:
        return

    pm_id, brand, last4 = _saved_card(session.get("payment_intent"))
    plan.deposit_status = PaymentPlan.DepositStatus.PAID
    plan.stripe_payment_method_id = pm_id
    plan.card_brand = brand
    plan.card_last4 = last4
    plan.balance_status = PaymentPlan.BalanceStatus.SCHEDULED
    plan.save(
        update_fields=[
            "deposit_status",
            "stripe_payment_method_id",
            "card_brand",
            "card_last4",
            "balance_status",
            "updated_at",
        ]
    )

    # Record the deposit Charge as captured and post the ledger entry.
    charge = plan.charges.filter(kind=Charge.Kind.DEPOSIT).first()
    if charge is None:
        charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    if charge.status != Charge.Status.SUCCEEDED:
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
    ledger.post_capture(
        lead=plan.lead,
        amount=plan.deposit_amount,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        stripe_ref=session.get("payment_intent") or "",
        memo="Deposit captured",
    )

    # Auto-book on deposit.
    lead = plan.lead
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status", "updated_at"])
    # TODO: trigger the Zapier -> LimoAnywhere sync (one Create Reservation per trip).


def _saved_card(payment_intent_id):
    if not payment_intent_id:
        return "", "", ""
    intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
    pm = getattr(intent, "payment_method", None)
    if not pm:
        return "", "", ""
    card = getattr(pm, "card", None)
    return pm.id, (card.brand if card else ""), (card.last4 if card else "")


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
