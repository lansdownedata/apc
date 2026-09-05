"""Stripe payments service — deposit checkout (saves the card) + off-session
balance charge. Card data never touches our servers (Checkout + tokens)."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.messaging import touchpoints
from apps.messaging.models import TouchPoint
from apps.notifications.models import Notification

from . import ledger
from .models import Charge, JournalEntry, PaymentPlan

ZERO = Decimal("0.00")

# How long we assume a deposit authorization stays capturable (APC-26). The card networks
# set the real ceiling — commonly ~7 days on credit, often less on debit — so this drives
# the staff countdown and the expiry sweep, never the decision to attempt a capture.
AUTH_HOLD_DAYS = 7


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


def open_intent_for(plan: PaymentPlan, *, kind: str, amount) -> tuple[Charge, str]:
    """Reuse this plan's open intent for `kind` at `amount`, else create one.

    `record_charge()` mints a row per call, so a public endpoint calling it per page load
    would let a token-holder pile up unbounded PENDING charges. Reuse closes that.

    The check is deliberately local — no `PaymentIntent.retrieve`. A spent intent is already
    caught by the PENDING filter (both the customer's `complete` POST and the webhook flip the
    Charge to SUCCEEDED), we never cancel deposit/balance intents, and a genuinely dead one
    only makes `confirmPayment` error client-side, which a refresh clears. That is worth not
    paying a Stripe round-trip on every hit of a public endpoint.

    A changed amount means a partial payment landed between page loads: a fresh Charge (and so
    a fresh idempotency key) is created and the old PENDING one is abandoned, not cancelled —
    Stripe expires unconfirmed intents on its own.
    """
    amount = Decimal(amount)
    existing = (
        plan.charges.filter(kind=kind, status=Charge.Status.PENDING, amount=amount)
        .exclude(stripe_payment_intent_id="")
        .exclude(stripe_client_secret="")
        .first()
    )
    if existing is not None:
        return existing, existing.stripe_client_secret

    customer = get_or_create_customer(plan)
    charge = plan.record_charge(kind=kind, amount=amount)
    extra = {}
    if kind == Charge.Kind.DEPOSIT:
        # APC-26: the deposit only *holds* at checkout. Availability can shift between
        # quote and booking, so nothing moves until a human at APC confirms the order.
        # A balance charge is money already owed on a confirmed booking — it still
        # captures automatically.
        extra["capture_method"] = "manual"
    intent = _stripe().PaymentIntent.create(
        amount=_cents(amount),
        currency="usd",
        customer=customer,
        payment_method_types=["card"],
        setup_future_usage="off_session",
        metadata={
            "lead_id": str(plan.lead_id),
            "kind": Charge.Kind(kind).value,
            "charge_id": str(charge.pk),
        },
        idempotency_key=charge.idempotency_key,
        **extra,
    )
    charge.stripe_payment_intent_id = intent.id
    charge.stripe_client_secret = intent.client_secret
    charge.save(update_fields=["stripe_payment_intent_id", "stripe_client_secret", "updated_at"])
    return charge, intent.client_secret


def create_deposit_intent(plan: PaymentPlan) -> tuple[Charge, str]:
    """The deposit intent for our own pay page. Saves the card for the balance cron."""
    charge, secret = open_intent_for(plan, kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    if plan.deposit_status != PaymentPlan.DepositStatus.REQUESTED:
        plan.deposit_status = PaymentPlan.DepositStatus.REQUESTED
        plan.save(update_fields=["deposit_status", "updated_at"])
    return charge, secret


def charge_balance(plan: PaymentPlan) -> Charge:
    """Charge the saved card off-session for the balance (idempotent)."""
    charge = plan.record_charge(kind=Charge.Kind.BALANCE, amount=plan.balance_amount)
    try:
        intent = _stripe().PaymentIntent.create(
            amount=_cents(plan.balance_amount),
            currency="usd",
            customer=plan.stripe_customer_id,
            payment_method=plan.stripe_payment_method_id,
            payment_method_types=["card"],
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

    if plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED:
        from apps.messaging.touchpoints import schedule_payment_reminder

        schedule_payment_reminder(plan.lead)
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
    """Create a PaymentIntent for the staff Payment Element. Returns (charge, client_secret).

    Routed through `open_intent_for`, which stops a repeated attempt minting a row per try.
    """
    amount = _parse_positive_amount(amount)
    return open_intent_for(plan, kind=Charge.Kind.BALANCE, amount=amount)


def create_setup_intent(plan: PaymentPlan) -> str:
    """Client secret so staff can save a card without charging."""
    customer = get_or_create_customer(plan)
    intent = _stripe().SetupIntent.create(
        customer=customer, usage="off_session", payment_method_types=["card"]
    )
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


def record_payment(
    plan: PaymentPlan, payment_intent_id: str, *, kind: str = Charge.Kind.BALANCE
) -> Charge:
    """Reconcile a succeeded PaymentIntent: ledger, card, statuses, maybe book.

    The single reconcile for all three surfaces — the customer pay page, the staff Payment
    Element, and the webhook. `kind` selects the Charge kind and the matching journal entry;
    everything else is identical, which is the point.

    The `expand=["payment_method"]` retrieve is load-bearing, not redundant: webhook payloads
    carry `payment_method` as a bare ID (expanded payloads are opt-in and we don't use them),
    so this is what resolves the card on every path.

    Plan flags are left to `sync_plan_from_collected` — it already sets deposit PAID +
    balance SCHEDULED once collected covers the deposit, so a deposit needs no special case.
    """
    from apps.leads.models import Lead
    from apps.leads.services import book_lead

    intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
    if intent.status != "succeeded":
        raise PaymentError(f"Payment has not succeeded ({intent.status}).")

    charge = plan.charges.filter(stripe_payment_intent_id=payment_intent_id).first()
    if charge is None:
        amount = (Decimal(intent.amount) / Decimal(100)).quantize(Decimal("0.01"))
        charge = plan.record_charge(kind=kind, amount=amount)
        charge.stripe_payment_intent_id = payment_intent_id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    if charge.status != Charge.Status.SUCCEEDED:
        is_deposit = charge.kind == Charge.Kind.DEPOSIT
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
        ledger.post_capture(
            lead=plan.lead,
            amount=charge.amount,
            kind=(
                JournalEntry.Kind.DEPOSIT_CAPTURED
                if is_deposit
                else JournalEntry.Kind.BALANCE_CAPTURED
            ),
            idempotency_key=f"capture-charge{charge.pk}",
            charge=charge,
            stripe_ref=payment_intent_id,
            memo="Deposit captured" if is_deposit else "Card payment",
        )

    _store_card(plan, getattr(intent, "payment_method", None))
    sync_plan_from_collected(plan)
    plan.lead.refresh_from_db()
    # ENGAGED is here because capture is exactly the moment a confirmed order becomes a
    # booking (APC-26) — `confirm_order` routes through this same tail.
    if plan.lead.status in (Lead.Status.NEW, Lead.Status.QUOTED, Lead.Status.ENGAGED):
        book_lead(plan.lead)
    return charge


# --- authorize → confirm → capture (APC-26) -------------------------------------------


def record_authorization(plan: PaymentPlan, payment_intent_id: str) -> Charge:
    """Reconcile a deposit authorization: hold recorded, card stored, lead ENGAGED.

    The mirror of `record_payment` for the half of the flow where *no money moves*. Called
    from the customer's inline `complete` POST and from the
    `payment_intent.amount_capturable_updated` webhook — under manual capture that event,
    not `succeeded`, is what says the customer paid.

    Deliberately posts **no ledger entry**: an authorization is not revenue, not cash, and
    may never become either. The books only learn about it at capture.
    """
    from apps.leads.models import Lead

    intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
    if intent.status not in ("requires_capture", "succeeded"):
        raise PaymentError(f"Payment has not been authorized ({intent.status}).")

    charge = plan.charges.filter(stripe_payment_intent_id=payment_intent_id).first()
    if charge is None:
        amount = (Decimal(intent.amount) / Decimal(100)).quantize(Decimal("0.01"))
        charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=amount)
        charge.stripe_payment_intent_id = payment_intent_id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    if charge.status == Charge.Status.PENDING:
        now = timezone.now()
        charge.status = Charge.Status.AUTHORIZED
        charge.authorized_at = now
        charge.capture_expires_at = now + timedelta(days=AUTH_HOLD_DAYS)
        charge.save(
            update_fields=[
                "status",
                "authorized_at",
                "capture_expires_at",
                "updated_at",
            ]
        )

    _store_card(plan, getattr(intent, "payment_method", None))
    if plan.deposit_status != PaymentPlan.DepositStatus.PAID:
        plan.deposit_status = PaymentPlan.DepositStatus.AUTHORIZED
        plan.save(update_fields=["deposit_status", "updated_at"])

    lead = plan.lead
    lead.refresh_from_db()
    if lead.status in (Lead.Status.NEW, Lead.Status.QUOTED):
        lead.status = Lead.Status.ENGAGED
        lead.save(update_fields=["status", "updated_at"])
        # The customer has done their part — stop the quote-chasing program.
        touchpoints.cancel_pending(lead)
    return charge


def _authorized_deposit(plan: PaymentPlan) -> Charge | None:
    return plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.AUTHORIZED).first()


def confirm_order(lead, *, user=None) -> Charge:
    """APC verified availability — capture the held deposit and book the order (APC-26).

    Everything downstream of "booked" is untouched: `record_payment` posts the ledger
    entry, stores the card, schedules the balance, pushes to LimoAnywhere and schedules the
    service-date touch-points via `book_lead`. Confirm just decides *when* that happens.

    Idempotent: an already-captured order returns its charge rather than double-capturing.
    """
    plan = getattr(lead, "payment", None)
    if plan is None:
        raise PaymentError("This order has no payment plan.")
    charge = _authorized_deposit(plan)
    if charge is None:
        captured = plan.charges.filter(
            kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED
        ).first()
        if captured is not None:
            return captured
        raise PaymentError("This order has no authorized deposit to capture.")

    _stripe().PaymentIntent.capture(charge.stripe_payment_intent_id)
    charge.captured_at = timezone.now()
    charge.save(update_fields=["captured_at", "updated_at"])
    # PENDING so `record_payment`'s own guard does the ledger post exactly once.
    charge.status = Charge.Status.PENDING
    charge.save(update_fields=["status", "updated_at"])
    captured = record_payment(plan, charge.stripe_payment_intent_id, kind=Charge.Kind.DEPOSIT)
    # After book_lead, so the confirmation isn't swept up by its cancel_pending.
    lead.refresh_from_db()
    touchpoints.trigger_order_decided(lead, confirmed=True)
    return captured


def cancel_order(lead, *, user=None, reason: str = "") -> Charge:
    """APC could not cover the trip — release the hold and lose the lead (APC-26).

    `PaymentIntent.cancel()` returns the held funds; no money ever moved, so there is
    nothing to refund and nothing for the ledger to say.
    """
    from apps.leads.models import Lead

    plan = getattr(lead, "payment", None)
    if plan is None:
        raise PaymentError("This order has no payment plan.")
    charge = _authorized_deposit(plan)
    if charge is None:
        raise PaymentError("This order has no authorized deposit to release.")

    _stripe().PaymentIntent.cancel(charge.stripe_payment_intent_id)
    charge.status = Charge.Status.RELEASED
    charge.save(update_fields=["status", "updated_at"])

    plan.deposit_status = PaymentPlan.DepositStatus.REQUESTED
    plan.save(update_fields=["deposit_status", "updated_at"])

    lead.status = Lead.Status.LOST
    lead.lost_reason = (reason or "Could not confirm availability")[:255]
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    touchpoints.cancel_pending(lead, kinds=list(TouchPoint.Kind.values))
    # Queued *after* the sweep above, which cancels every pending kind — the message
    # explaining the cancellation must not be cancelled by the cancellation.
    touchpoints.trigger_order_decided(lead, confirmed=False)
    return charge


def expire_authorization(charge: Charge) -> Charge:
    """The issuer released a hold before APC confirmed the order (APC-26, Exception 1).

    Nobody is at fault and no money moved, so this is a *reversal of state, not of money*:
    no ledger entry, no refund. The order stops being engaged and becomes a live quote
    again, because the one thing the customer needs is a working way to book it a second
    time — with an explanation, which `trigger_auth_expired` carries.
    """
    from apps.leads.models import Lead
    from apps.leads.services import compute_quote_expiry

    plan = charge.plan
    # The sweep's row was read before its Stripe round-trip, and `cancel_order` cancels the
    # intent *then* saves RELEASED — so a hold we released ourselves reads as `canceled` at
    # Stripe while this in-memory row still says AUTHORIZED. Re-read before believing it, or
    # the customer gets both "we cancelled your trip" and "your bank released the hold".
    charge.refresh_from_db()
    if charge.status != Charge.Status.AUTHORIZED:
        return charge

    charge.status = Charge.Status.EXPIRED
    charge.save(update_fields=["status", "updated_at"])
    # Only ever a downgrade from "we hold their money". A plan that is already PAID has a
    # separate captured deposit; a stale hold lapsing beside it must not reopen the ask.
    if plan.deposit_status != PaymentPlan.DepositStatus.PAID:
        plan.deposit_status = PaymentPlan.DepositStatus.REQUESTED
        plan.save(update_fields=["deposit_status", "updated_at"])

    lead = plan.lead
    lead.refresh_from_db()
    if lead.status == Lead.Status.ENGAGED:
        lead.status = Lead.Status.QUOTED
        # A quote that expired while we sat on it must not greet them with "expired" —
        # they did their part. Give it a fresh window to act in.
        lead.quote_expires_at = compute_quote_expiry(lead)
        lead.save(update_fields=["status", "quote_expires_at", "updated_at"])
        touchpoints.trigger_auth_expired(lead)

    Notification.notify(
        lead,
        Notification.Kind.AUTH_EXPIRED,
        title=f"Deposit hold released — {lead.quote_no}",
        detail=(
            f"${charge.amount:,.2f} was never captured and the bank has released it. "
            "The quote is live again and the customer has been asked to re-authorize."
        ),
    )
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
            payment_method_types=["card"],
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
    return record_payment(plan, intent.id)


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
