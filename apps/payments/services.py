"""Stripe payments service — deposit checkout (saves the card) + off-session
balance charge. Card data never touches our servers (Checkout + tokens)."""

from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction

from apps.notifications.models import Notification

from . import ledger
from .models import Charge, JournalEntry, PaymentPlan

ZERO = Decimal("0.00")


class PaymentError(Exception):
    """User-facing payment failure (bad amount, incomplete intent, no card)."""


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

    with transaction.atomic():
        charge.stripe_payment_intent_id = intent.id
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
        plan.balance_status = PaymentPlan.BalanceStatus.PAID
        plan.save(update_fields=["balance_status", "updated_at"])
        ledger.post_capture(
            lead=plan.lead,
            amount=plan.balance_amount,
            kind=JournalEntry.Kind.BALANCE_CAPTURED,
            idempotency_key=f"capture-charge{charge.pk}",
            charge=charge,
            stripe_ref=intent.id,
            memo="Balance captured",
        )
    return charge


def refund_payment(plan, amount):
    """Refund `amount` to the card — balance PI first, then deposit PI. Real Stripe refund.

    Caps each charge at its remaining refundable amount (captured − already refunded) so a
    repeated/duplicate call cannot over-refund, and records each Stripe refund + its ledger
    entry atomically so the books never desync from a recorded refund.
    """
    from decimal import Decimal

    from django.db.models import Sum

    amount = Decimal(amount)
    remaining = amount
    total_refunded = Decimal("0.00")
    succeeded = {
        c.kind: c
        for c in plan.charges.filter(
            kind__in=[Charge.Kind.DEPOSIT, Charge.Kind.BALANCE],
            status=Charge.Status.SUCCEEDED,
        )
    }
    for kind in (Charge.Kind.BALANCE, Charge.Kind.DEPOSIT):
        if remaining <= Decimal("0.00"):
            break
        src = succeeded.get(kind)
        if not src or not src.stripe_payment_intent_id:
            continue
        already = plan.charges.filter(
            kind=Charge.Kind.REFUND,
            stripe_payment_intent_id=src.stripe_payment_intent_id,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
        refundable = src.amount - already
        if refundable <= Decimal("0.00"):
            continue
        portion = min(remaining, refundable)
        refund = _stripe().Refund.create(
            payment_intent=src.stripe_payment_intent_id,
            amount=_cents(portion),
            idempotency_key=f"refund-{src.pk}-{_cents(already)}-{_cents(portion)}",
        )
        try:
            with transaction.atomic():
                charge = plan.charges.create(
                    kind=Charge.Kind.REFUND,
                    amount=portion,
                    status=Charge.Status.SUCCEEDED,
                    stripe_payment_intent_id=src.stripe_payment_intent_id,
                    stripe_refund_id=refund.id,
                    idempotency_key=f"refund-{refund.id}",
                )
                ledger.post_refund(
                    lead=plan.lead,
                    amount=portion,
                    charge=charge,
                    idempotency_key=f"refund-{refund.id}",
                    memo="Refund",
                )
        except IntegrityError:
            # A concurrent request already recorded this exact Stripe refund — don't double-count.
            continue
        total_refunded += portion
        remaining -= portion
    return total_refunded


def remaining_balance(lead) -> Decimal:
    """Quote total minus collected cash. Never negative."""
    plan = getattr(lead, "payment", None)
    total = Decimal(plan.quote_total if plan is not None else lead.quote_total)
    leftover = total - ledger.order_balances(lead)["collected"]
    return leftover if leftover > ZERO else ZERO


def ensure_plan(lead) -> PaymentPlan:
    """PaymentPlan for this lead, snapshotting quote total if new or still $0."""
    plan, created = PaymentPlan.objects.get_or_create(lead=lead)
    if created or plan.quote_total == 0:
        plan.snapshot_total()
    return plan


def sync_plan_from_collected(plan: PaymentPlan) -> None:
    """Flip deposit/balance flags from ledger collected vs the snapshotted total."""
    collected = ledger.order_balances(plan.lead)["collected"]
    total = Decimal(plan.quote_total)
    fields = ["updated_at"]
    if total > ZERO and collected >= total:
        plan.deposit_status = PaymentPlan.DepositStatus.PAID
        plan.balance_status = PaymentPlan.BalanceStatus.PAID
        fields += ["deposit_status", "balance_status"]
    elif collected >= plan.deposit_amount and plan.deposit_amount > ZERO:
        plan.deposit_status = PaymentPlan.DepositStatus.PAID
        if plan.balance_status == PaymentPlan.BalanceStatus.NA:
            plan.balance_status = PaymentPlan.BalanceStatus.SCHEDULED
        fields += ["deposit_status", "balance_status"]
    plan.save(update_fields=fields)
    if plan.lead.has_alert and plan.balance_status != PaymentPlan.BalanceStatus.FAILED:
        plan.lead.has_alert = False
        plan.lead.save(update_fields=["has_alert", "updated_at"])


def _parse_positive_amount(amount) -> Decimal:
    try:
        value = Decimal(amount)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentError("Enter a valid amount.") from exc
    if value <= ZERO:
        raise PaymentError("Enter an amount greater than zero.")
    return value


def _store_card(plan: PaymentPlan, payment_method) -> None:
    if payment_method is None:
        return
    plan.stripe_payment_method_id = payment_method.id
    card = getattr(payment_method, "card", None)
    plan.card_brand = card.brand if card else ""
    plan.card_last4 = card.last4 if card else ""
    plan.save(
        update_fields=[
            "stripe_payment_method_id",
            "card_brand",
            "card_last4",
            "updated_at",
        ]
    )


def create_admin_payment_intent(plan: PaymentPlan, amount) -> tuple[Charge, str]:
    """Create a PaymentIntent for the staff Payment Element. Returns (charge, client_secret)."""
    amount = _parse_positive_amount(amount)
    customer = get_or_create_customer(plan)
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=amount)
    intent = _stripe().PaymentIntent.create(
        amount=_cents(amount),
        currency="usd",
        customer=customer,
        setup_future_usage="off_session",
        metadata={
            "lead_id": str(plan.lead_id),
            "kind": "admin",
            "charge_id": str(charge.pk),
        },
        idempotency_key=charge.idempotency_key,
    )
    charge.stripe_payment_intent_id = intent.id
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return charge, intent.client_secret


def create_setup_intent(plan: PaymentPlan) -> str:
    """Client secret so staff can save a card without charging."""
    customer = get_or_create_customer(plan)
    intent = _stripe().SetupIntent.create(customer=customer, usage="off_session")
    return intent.client_secret


def save_payment_method(plan: PaymentPlan, payment_method_id: str) -> PaymentPlan:
    """Attach a Stripe PaymentMethod to the plan's customer and store brand/last4."""
    customer = get_or_create_customer(plan)
    pm = _stripe().PaymentMethod.retrieve(payment_method_id)
    attached_to = getattr(pm, "customer", None)
    if attached_to != customer:
        _stripe().PaymentMethod.attach(payment_method_id, customer=customer)
    _store_card(plan, pm)
    return plan


def record_admin_payment(plan: PaymentPlan, payment_intent_id: str) -> Charge:
    """Reconcile a succeeded staff PaymentIntent: ledger, card, statuses, maybe book."""
    from apps.leads.models import Lead
    from apps.leads.services import book_lead

    intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
    if intent.status != "succeeded":
        raise PaymentError(f"Payment has not succeeded ({intent.status}).")

    charge = plan.charges.filter(stripe_payment_intent_id=payment_intent_id).first()
    if charge is None:
        amount = (Decimal(intent.amount) / Decimal(100)).quantize(Decimal("0.01"))
        charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=amount)
        charge.stripe_payment_intent_id = payment_intent_id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    if charge.status != Charge.Status.SUCCEEDED:
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
        ledger.post_capture(
            lead=plan.lead,
            amount=charge.amount,
            kind=JournalEntry.Kind.BALANCE_CAPTURED,
            idempotency_key=f"capture-charge{charge.pk}",
            charge=charge,
            stripe_ref=payment_intent_id,
            memo="Admin card payment",
        )

    _store_card(plan, getattr(intent, "payment_method", None))
    sync_plan_from_collected(plan)
    plan.lead.refresh_from_db()
    if plan.lead.status == Lead.Status.QUOTED:
        book_lead(plan.lead)
    return charge


def charge_saved_card(plan: PaymentPlan, amount) -> Charge:
    """Off-session charge of the card already on the plan."""
    amount = _parse_positive_amount(amount)
    if not plan.stripe_payment_method_id or not plan.stripe_customer_id:
        raise PaymentError("No card on file.")
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=amount)
    try:
        intent = _stripe().PaymentIntent.create(
            amount=_cents(amount),
            currency="usd",
            customer=plan.stripe_customer_id,
            payment_method=plan.stripe_payment_method_id,
            off_session=True,
            confirm=True,
            metadata={
                "lead_id": str(plan.lead_id),
                "kind": "admin",
                "charge_id": str(charge.pk),
            },
            idempotency_key=charge.idempotency_key,
        )
    except stripe.error.CardError as exc:
        return _record_failure(plan, charge, exc)
    charge.stripe_payment_intent_id = intent.id
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return record_admin_payment(plan, intent.id)


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
    Notification.notify(
        lead, Notification.Kind.BALANCE_FAILED, title="Balance charge failed", detail=reason
    )
    return charge
