"""Stripe payments service — deposit checkout (saves the card) + off-session
balance charge. Card data never touches our servers (Checkout + tokens)."""

import stripe
from django.conf import settings

from .models import Charge, PaymentPlan


def _stripe():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _cents(amount) -> int:
    return int(round(float(amount) * 100))


def get_or_create_customer(plan: PaymentPlan) -> str:
    if plan.stripe_customer_id:
        return plan.stripe_customer_id
    contact = plan.lead.contact
    customer = _stripe().Customer.create(
        name=contact.name,
        email=contact.email or None,
        phone=contact.phone or None,
        metadata={"lead_id": plan.lead_id, "quote_no": plan.lead.quote_no},
    )
    plan.stripe_customer_id = customer.id
    plan.save(update_fields=["stripe_customer_id", "updated_at"])
    return customer.id


def create_deposit_checkout(plan: PaymentPlan, *, success_url: str, cancel_url: str) -> str:
    """A Checkout Session for the deposit that also saves the card for the balance."""
    customer = get_or_create_customer(plan)
    session = _stripe().checkout.Session.create(
        mode="payment",
        customer=customer,
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"Deposit · {plan.lead.quote_no}"},
                    "unit_amount": _cents(plan.deposit_amount),
                },
                "quantity": 1,
            }
        ],
        payment_intent_data={"setup_future_usage": "off_session"},
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"lead_id": plan.lead_id, "kind": Charge.Kind.DEPOSIT.value},
    )
    plan.deposit_status = PaymentPlan.DepositStatus.REQUESTED
    plan.save(update_fields=["deposit_status", "updated_at"])
    return session.url


def charge_balance(plan: PaymentPlan) -> Charge:
    """Charge the saved card off-session for the balance (idempotent)."""
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=plan.balance_amount)
    try:
        intent = _stripe().PaymentIntent.create(
            amount=_cents(plan.balance_amount),
            currency="usd",
            customer=plan.stripe_customer_id,
            payment_method=plan.stripe_payment_method_id,
            off_session=True,
            confirm=True,
            metadata={
                "lead_id": plan.lead_id,
                "kind": Charge.Kind.BALANCE.value,
                "charge_id": charge.id,
            },
            idempotency_key=charge.idempotency_key,
        )
    except stripe.error.CardError as exc:
        return _record_failure(plan, charge, exc)

    charge.stripe_payment_intent_id = intent.id
    charge.status = Charge.Status.SUCCEEDED
    charge.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    plan.balance_status = PaymentPlan.BalanceStatus.PAID
    plan.save(update_fields=["balance_status", "updated_at"])
    return charge


def _record_failure(plan: PaymentPlan, charge: Charge, exc: Exception) -> Charge:
    reason = (getattr(exc, "user_message", None) or str(exc))[:255]
    charge.status = Charge.Status.FAILED
    charge.failure_reason = reason
    charge.save(update_fields=["status", "failure_reason", "updated_at"])

    plan.balance_status = PaymentPlan.BalanceStatus.FAILED
    plan.fail_reason = reason
    plan.save(update_fields=["balance_status", "fail_reason", "updated_at"])

    lead = plan.lead
    lead.has_alert = True
    lead.save(update_fields=["has_alert", "updated_at"])
    # TODO: create Notification(balance_failed) once the notifications model exists.
    return charge
