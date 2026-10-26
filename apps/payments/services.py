"""Stripe payments service — deposit authorization + balance charge, both on our own
pages. Card data never touches our servers: it is entered into Stripe's own iframes and
we only ever hold a PaymentMethod id (hosted Checkout was retired 2026-08-30)."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core import outbound
from apps.core.choices import Account
from apps.messaging import touchpoints
from apps.messaging.models import TouchPoint
from apps.notifications.models import Notification
from apps.reservations.models import Reservation

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
    """The Stripe customer this order charges. An order that already has one keeps it (orders
    from before the card vault each made their own, and Stripe can't move a card between
    customers). A new order uses the contact's customer, so the cards it is paid with land in
    the customer's vault (APC-47). It never picks up a saved card by itself — an order holds
    a card once the customer pays on it (Moe, 2026-10-24)."""
    from . import cards

    if plan.stripe_customer_id:
        return plan.stripe_customer_id
    plan.stripe_customer_id = cards.contact_customer(plan.lead.contact)
    plan.save(update_fields=["stripe_customer_id", "updated_at"])
    return plan.stripe_customer_id


def open_intent_for(
    plan: PaymentPlan, *, kind: str, amount, reservation=None, **extra
) -> tuple[Charge, str]:
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
        plan.charges.filter(
            kind=kind, status=Charge.Status.PENDING, amount=amount, reservation=reservation
        )
        .exclude(stripe_payment_intent_id="")
        .exclude(stripe_client_secret="")
        .first()
    )
    if existing is not None:
        lines = extra.get("receipt_lines")
        note = extra.get("note", existing.note)
        if lines is not None and (existing.receipt_lines != lines or existing.note != note):
            # The same amount for different reasons: the receipt says what it is for now.
            existing.receipt_lines, existing.note = lines, note
            existing.save(update_fields=["receipt_lines", "note", "updated_at"])
        return existing, existing.stripe_client_secret

    customer = get_or_create_customer(plan)
    charge = plan.record_charge(kind=kind, amount=amount, reservation=reservation, **extra)
    metadata = {
        "lead_id": str(plan.lead_id),
        "kind": Charge.Kind(kind).value,
        "charge_id": str(charge.pk),
    }
    if reservation is not None:
        metadata["reservation_id"] = str(reservation.pk)
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
        metadata=metadata,
        idempotency_key=charge.idempotency_key,
        **extra,
    )
    charge.stripe_payment_intent_id = intent.id
    charge.stripe_client_secret = intent.client_secret
    charge.save(update_fields=["stripe_payment_intent_id", "stripe_client_secret", "updated_at"])
    return charge, intent.client_secret


def charge_balance(plan: PaymentPlan) -> Charge:
    """Charge the saved card off-session for the balance (idempotent).

    Clamped to what the order still owes: once trips are charged on their own (APC-101) the
    frozen `balance_amount` can exceed the live remainder, and a retry must not collect
    money the trips already did."""
    amount = min(Decimal(plan.balance_amount), remaining_balance(plan.lead))
    if amount <= ZERO:
        sync_plan_from_collected(plan)  # the flags catch up: this balance is settled
        raise PaymentError("Nothing is owed on this order.")
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
                "lead_id": plan.lead_id,
                "kind": Charge.Kind.BALANCE.value,
                "charge_id": charge.id,
            },
            idempotency_key=charge.idempotency_key,
        )
    except (stripe.error.CardError, stripe.error.InvalidRequestError) as exc:
        # InvalidRequestError: the saved card is gone (detached) — a failed balance the
        # office must see, not an unhandled error that leaves a PENDING row (APC-47).
        return _record_failure(plan, charge, exc)

    with transaction.atomic():
        charge.stripe_payment_intent_id = intent.id
        charge.status = Charge.Status.SUCCEEDED
        # This path confirms off-session and posts its own ledger entry, so it never
        # reaches `record_payment` and the APC-40 snapshot there. It charges
        # `plan.stripe_payment_method_id` by definition, so the card on the plan right now
        # *is* the card that paid — no Stripe round-trip needed to learn it.
        charge.card_brand = plan.card_brand
        charge.card_last4 = plan.card_last4
        charge.save(
            update_fields=[
                "stripe_payment_intent_id",
                "status",
                "card_brand",
                "card_last4",
                "updated_at",
            ]
        )
        plan.balance_status = PaymentPlan.BalanceStatus.PAID
        plan.save(update_fields=["balance_status", "updated_at"])
        ledger.post_capture(
            lead=plan.lead,
            amount=amount,
            kind=JournalEntry.Kind.BALANCE_CAPTURED,
            idempotency_key=f"capture-charge{charge.pk}",
            charge=charge,
            stripe_ref=intent.id,
            memo="Balance captured",
        )
    # Not through record_payment, so it needs its own hook — or every cron-charged
    # balance would leave "Final balance paid" open until the next run-tasks tick.
    from apps.tasks import services as tasks

    tasks.sync(plan.lead)
    return charge


# Newest money back first (Moe, 2026-10-24): the trip's own charges (APC-101), then a
# historical final bill, then the balance, then the deposit.
_REFUND_ORDER = (Charge.Kind.TRIP, Charge.Kind.FINAL, Charge.Kind.BALANCE, Charge.Kind.DEPOSIT)


def _refunded_on(plan, payment_intent_id: str) -> Decimal:
    from django.db.models import Sum

    return plan.charges.filter(
        kind=Charge.Kind.REFUND, stripe_payment_intent_id=payment_intent_id
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")


def _refundable_sources(plan, *, trip=None) -> list[tuple[Charge, Decimal]]:
    """(charge, still refundable) for every succeeded Stripe charge, in refund order. With
    `trip`, only that trip's TRIP charges count (another trip's money is not this trip's to
    return); without, every TRIP charge counts, newest first."""
    rank = {kind: i for i, kind in enumerate(_REFUND_ORDER)}
    charges = plan.charges.filter(kind__in=_REFUND_ORDER, status=Charge.Status.SUCCEEDED)
    if trip is not None:
        charges = charges.exclude(kind=Charge.Kind.TRIP) | charges.filter(
            kind=Charge.Kind.TRIP, reservation=trip
        )
    out = []
    for src in sorted(charges, key=lambda c: (rank[c.kind], -c.pk)):
        if not src.stripe_payment_intent_id:
            continue
        left = Decimal(src.amount) - _refunded_on(plan, src.stripe_payment_intent_id)
        if left > Decimal("0.00"):
            out.append((src, left))
    return out


def _paid_back_off_card(plan) -> Decimal:
    """Refunds recorded as paid back outside Stripe (check, ACH, Zelle, other — APC-102)."""
    agg = (
        plan.charges.filter(kind=Charge.Kind.REFUND, status=Charge.Status.SUCCEEDED)
        .exclude(refund_method__in=("", Charge.RefundMethod.CARD))
        .aggregate(s=Sum("amount"))
    )
    return Decimal(agg["s"] or 0)


def refundable_total(plan, *, trip=None) -> Decimal:
    """Everything still refundable to the card, trip charges and the final bill included
    (APC-75, APC-101) — less money already paid back off-card, which the card must never
    return a second time (APC-102)."""
    on_card = sum((left for _src, left in _refundable_sources(plan, trip=trip)), Decimal("0.00"))
    return max(on_card - _paid_back_off_card(plan), Decimal("0.00"))


def record_manual_refund(
    plan,
    amount,
    *,
    method: str,
    reservation=None,
    reference: str = "",
    note: str = "",
    user=None,
    receipt: bool = False,
) -> Decimal:
    """Money paid back outside Stripe — a check, ACH, Zelle (APC-102). Nothing is sent
    anywhere: the refund is recorded with how it went back and its reference, and posted to
    the books exactly as a card refund is (Cash out; Refunds for a recognised trip, else
    Customer Deposits). The same refund recorded twice in a row is refused (a double-click).
    """
    from uuid import uuid4

    amount = Decimal(amount).quantize(Decimal("0.01"))
    reference = (reference or "").strip()[:120]
    with transaction.atomic():
        PaymentPlan.objects.select_for_update().get(pk=plan.pk)
        duplicate = plan.charges.filter(
            kind=Charge.Kind.REFUND,
            reservation=reservation,
            amount=amount,
            refund_method=method,
            reference=reference,
            created_at__gte=timezone.now() - timedelta(minutes=2),
        ).exists()
        if duplicate:
            raise PaymentError("That refund was just recorded.")
        key = f"manual-refund-{plan.pk}-{uuid4().hex[:16]}"
        charge = plan.charges.create(
            kind=Charge.Kind.REFUND,
            amount=amount,
            status=Charge.Status.SUCCEEDED,
            refund_method=method,
            reference=reference,
            reservation=reservation,
            note=(note or "")[:255],
            idempotency_key=key,
            receipt_due_at=timezone.now() if receipt else None,
        )
        memo = f"Refund by {Charge.RefundMethod(method).label}" + (
            f" {reference}" if reference else ""
        )
        recognised = (
            reservation is not None
            and reservation.revenue_status == Reservation.RevenueStatus.RECOGNIZED
        )
        if recognised:
            ledger.post_trip_refund(
                lead=plan.lead,
                reservation=reservation,
                amount=amount,
                charge=charge,
                idempotency_key=key,
                created_by=user,
                memo=memo,
            )
        else:
            ledger.post_refund(
                lead=plan.lead,
                amount=amount,
                charge=charge,
                idempotency_key=key,
                created_by=user,
                memo=memo,
            )
    if receipt:
        from . import receipts

        receipts.queue(charge)
    return amount


def _final_split(src: Charge, already: Decimal, portion: Decimal) -> tuple[Decimal, ...]:
    """(gratuity, base, balance part) of `portion` refunded against a FINAL charge, after
    `already` was refunded against it. Overtime first (Moe, 2026-10-24): the first dollars
    ever refunded on the charge come off the overtime gratuity, then the overtime base,
    then the balance part — so a second partial picks up exactly where the first stopped."""
    # Clamped so bad data (a gratuity above the overtime, overtime above the charge) can
    # never split into pieces that add up to more cash than Stripe refunded.
    total = Decimal(src.amount)
    overtime = min(max(Decimal(src.overtime_amount or 0), Decimal("0.00")), total)
    gratuity = min(max(Decimal(src.overtime_gratuity or 0), Decimal("0.00")), overtime)
    base = overtime - gratuity
    balance = total - overtime
    lo, hi = already, already + portion
    pieces, edge = [], Decimal("0.00")
    for size in (gratuity, base, balance):
        top = edge + size
        pieces.append(max(Decimal("0.00"), min(hi, top) - max(lo, edge)))
        edge = top
    return tuple(pieces)


def refund_payment(
    plan,
    amount,
    *,
    reservation=None,
    note: str = "",
    user=None,
    receipt: bool = False,
    recorded: list | None = None,
):
    """Refund `amount` to the card — this trip's charges (APC-101) or the final bill first,
    then the balance, then the deposit (APC-75). Real Stripe refunds. `reservation` and
    `note` are stamped on every REFUND row the call makes.

    Caps each charge at its remaining refundable amount (captured − already refunded) so a
    repeated/duplicate call cannot over-refund, and records each Stripe refund + its ledger
    entry atomically so the books never desync from a recorded refund. A refund against a
    final bill reverses its overtime's Recognized Revenue first, then its balance part the
    way a balance refund does.

    `receipt` (a trip refund from processing, APC-103): the first row this call creates
    carries the customer's receipt (`receipt_due_at`), the rest point at it, and it is sent
    once the refund is recorded. Order-console refunds pass nothing and send no receipt.
    `recorded`, when given, collects the rows this call itself created — a concurrent
    duplicate is counted in the return value but recorded by the other request.
    """
    from . import receipts

    # Never more than the card can still give back, net of anything paid back off-card.
    amount = min(Decimal(amount), refundable_total(plan, trip=reservation))
    remaining = amount
    heads: list[Charge] = []  # the first row this call creates: the receipt
    try:
        total_refunded = _refund_sources(
            plan,
            remaining,
            reservation=reservation,
            note=note,
            receipt=receipt,
            heads=heads,
            recorded=recorded if recorded is not None else [],
        )
    finally:
        # Even when a later Stripe call fails, the part that did move gets its receipt.
        if heads:
            receipts.queue(heads[0])
    return total_refunded


def _refund_sources(plan, remaining, *, reservation, note, receipt, heads, recorded) -> Decimal:
    total_refunded = Decimal("0.00")
    head = None
    for src, refundable in _refundable_sources(plan, trip=reservation):
        if remaining <= Decimal("0.00"):
            break
        already = Decimal(src.amount) - refundable
        portion = min(remaining, refundable)
        refund = _stripe().Refund.create(
            payment_intent=src.stripe_payment_intent_id,
            amount=_cents(portion),
            idempotency_key=f"refund-{src.pk}-{_cents(already)}-{_cents(portion)}",
        )
        try:
            with transaction.atomic():
                gratuity = base = Decimal("0.00")
                balance_part = portion
                if src.kind == Charge.Kind.FINAL:
                    gratuity, base, balance_part = _final_split(src, already, portion)
                # The refund is about the trip asked for, else the trip whose charge it
                # reverses (so an order-level refund of a trip charge still lands on that
                # trip's figures), else nobody's: order-level money.
                trip = reservation if reservation is not None else src.reservation
                charge = plan.charges.create(
                    kind=Charge.Kind.REFUND,
                    amount=portion,
                    status=Charge.Status.SUCCEEDED,
                    stripe_payment_intent_id=src.stripe_payment_intent_id,
                    stripe_refund_id=refund.id,
                    refund_method=Charge.RefundMethod.CARD,
                    idempotency_key=f"refund-{refund.id}",
                    overtime_amount=gratuity + base,
                    overtime_gratuity=gratuity,
                    reservation=trip,
                    note=note[:255],
                    # The card this money goes back to, for the receipt (APC-103).
                    card_brand=src.card_brand,
                    card_last4=src.card_last4,
                    receipt_due_at=timezone.now() if receipt and head is None else None,
                    receipt_parent=head if receipt else None,
                )
                if trip is not None and trip.revenue_status == Reservation.RevenueStatus.RECOGNIZED:
                    # Earned money going back: contra revenue, never other trips' deferred
                    # cash (APC-101).
                    ledger.post_trip_refund(
                        lead=plan.lead,
                        reservation=trip,
                        amount=portion,
                        charge=charge,
                        idempotency_key=f"refund-{refund.id}",
                        memo="Refund",
                    )
                    balance_part = Decimal("0.00")
                elif src.kind == Charge.Kind.FINAL:
                    if gratuity + base > Decimal("0.00"):
                        ledger.post_overtime_refund(
                            lead=plan.lead,
                            base=base,
                            gratuity=gratuity,
                            charge=charge,
                            idempotency_key=f"refund-{refund.id}-overtime",
                        )
                if balance_part > Decimal("0.00"):
                    ledger.post_refund(
                        lead=plan.lead,
                        amount=balance_part,
                        charge=charge,
                        idempotency_key=f"refund-{refund.id}",
                        memo="Refund",
                    )
        except IntegrityError:
            # A concurrent request sent Stripe the same key, got this same refund back, and
            # recorded it first. It is done: count it and stop here, never go on to refund
            # the next charge as well (a double-click must not refund twice).
            total_refunded += portion
            break
        recorded.append(charge)
        if receipt and head is None:
            head = charge
            heads.append(charge)
        total_refunded += portion
        remaining -= portion
    return total_refunded


def remaining_balance(lead) -> Decimal:
    """Total due minus collected cash. Never negative.

    Total due is the *live* order total — the sum of the trips' `line_total`, which carries
    overtime, extras and every refund's credit line on the trip itself (spec 2026-10-25
    §3, §4.7) — so a price edit on a booked order changes what is owed. The plan's
    `quote_total` snapshot stays the deposit basis only."""
    leftover = Decimal(lead.quote_total) - ledger.order_balances(lead)["collected"]
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


def _store_card(plan: PaymentPlan, payment_method, charge: Charge | None = None) -> None:
    """Update the card on file, and snapshot it onto the charge that used it.

    The two are not the same fact and must not be kept in one place. The plan holds the
    card the *next* charge will use, and `save_payment_method` overwrites it the moment the
    customer swaps cards. The charge holds what paid *it*, and must never move again.

    The charge is only written when a card actually came back. A webhook re-delivery can
    resolve no payment method at all, and a non-card method has no brand — neither is
    evidence the card changed, and writing "" for them would lose a card already recorded.
    """
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
    if charge is not None and (plan.card_brand or plan.card_last4):
        charge.card_brand = plan.card_brand
        charge.card_last4 = plan.card_last4
        charge.save(update_fields=["card_brand", "card_last4", "updated_at"])
    from . import cards

    cards.remember_card(plan, payment_method)  # APC-47: a card paid with joins the vault


def create_admin_payment_intent(plan: PaymentPlan, amount) -> tuple[Charge, str]:
    """Create a PaymentIntent for the staff Payment Element. Returns (charge, client_secret).

    Routed through `open_intent_for`, which stops a repeated attempt minting a row per try.
    """
    amount = _parse_positive_amount(amount)
    return open_intent_for(plan, kind=Charge.Kind.BALANCE, amount=amount)


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
    receipt_now = False
    from apps.leads.models import Lead
    from apps.leads.services import book_lead

    intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
    if intent.status != "succeeded":
        raise PaymentError(f"Payment has not succeeded ({intent.status}).")

    charge = plan.charges.filter(stripe_payment_intent_id=payment_intent_id).first()
    if charge is None:
        amount = (Decimal(intent.amount) / Decimal(100)).quantize(Decimal("0.01"))
        metadata = getattr(intent, "metadata", None)
        # Stripe objects are dict-like; a test double may not be.
        reservation_id = metadata.get("reservation_id") if isinstance(metadata, dict) else None
        charge = plan.record_charge(
            kind=kind,
            amount=amount,
            reservation_id=int(reservation_id) if reservation_id else None,
        )
        charge.stripe_payment_intent_id = payment_intent_id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    if charge.status != Charge.Status.SUCCEEDED and charge.kind == Charge.Kind.FINAL:
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
        _post_final(plan, charge, payment_intent_id)
    elif charge.status != Charge.Status.SUCCEEDED:
        is_deposit = charge.kind == Charge.Kind.DEPOSIT
        is_trip = charge.kind == Charge.Kind.TRIP
        charge.status = Charge.Status.SUCCEEDED
        # A trip charge is processing's money: its receipt is due from now (APC-103).
        if is_trip and charge.reservation_id and charge.receipt_due_at is None:
            charge.receipt_due_at = timezone.now()
            receipt_now = True
        charge.save(update_fields=["status", "receipt_due_at", "updated_at"])
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
            memo=(
                "Deposit captured"
                if is_deposit
                else f"Trip charge ({charge.reservation})"
                if is_trip
                else "Card payment"
            ),
            reservation=charge.reservation if is_trip else None,
        )

    _store_card(plan, getattr(intent, "payment_method", None), charge)
    sync_plan_from_collected(plan)
    if charge.kind == Charge.Kind.FINAL:
        _close_overtime_invoiced(plan.lead)
    elif charge.kind == Charge.Kind.TRIP and charge.reservation_id:
        _close_overtime_invoiced(plan.lead, reservation_id=charge.reservation_id)
        # The trip's revenue follows its live total (spec 2026-10-25 §4.4): a charge on an
        # already-recognised trip moves the new money out of deferred into revenue.
        ledger.true_up_recognition(charge.reservation)
    plan.lead.refresh_from_db()
    # ENGAGED is here because capture is exactly the moment a confirmed order becomes a
    # booking (APC-26) — `confirm_order` routes through this same tail.
    if plan.lead.status in (Lead.Status.NEW, Lead.Status.QUOTED, Lead.Status.ENGAGED):
        book_lead(plan.lead)  # syncs tasks itself
    else:
        from apps.tasks import services as tasks

        tasks.sync(plan.lead)
    if receipt_now:
        # Last, so the card `_store_card` just wrote is on the row the receipt reads.
        from . import receipts

        receipts.queue(charge)
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

    _store_card(plan, getattr(intent, "payment_method", None), charge)
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
                # "balance", matching the Charge recorded above, and NOT the "admin" this
                # carried until 2026-09-19: `webhooks._SUCCESS_KINDS` has no entry for
                # that, so `payment_intent.succeeded` returned early and the safety net
                # skipped the event. Invisible while the inline reconcile below works,
                # and exactly the money nobody finds on the one occasion it doesn't.
                "kind": "balance",
                "charge_id": str(charge.pk),
            },
            idempotency_key=charge.idempotency_key,
        )
    except stripe.error.CardError as exc:
        return _record_failure(plan, charge, exc)
    charge.stripe_payment_intent_id = intent.id
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return record_payment(plan, intent.id)


# --- the historical final bill (APC-60) -------------------------------------------------
# Nothing creates a FINAL charge since APC-101 (trips are charged on their own); an intent
# still in flight at deploy time reconciles through `record_payment` → `_post_final`.


def _post_final(plan: PaymentPlan, charge: Charge, payment_intent_id: str) -> None:
    """Split a final bill: the balance part posts exactly as a balance payment does, and
    the overtime goes straight to Recognized Revenue — base and gratuity as two lines,
    the way trip gratuity is revenue too. The trip has run, so nothing is deferred."""
    overtime = Decimal(charge.overtime_amount)
    gratuity = Decimal(charge.overtime_gratuity)
    balance_part = Decimal(charge.amount) - overtime
    if balance_part > ZERO:
        ledger.post_capture(
            lead=plan.lead,
            amount=balance_part,
            kind=JournalEntry.Kind.BALANCE_CAPTURED,
            idempotency_key=f"capture-charge{charge.pk}",
            charge=charge,
            stripe_ref=payment_intent_id,
            memo="Balance captured (final bill)",
        )
    if overtime > ZERO:
        lines = [
            (Account.CASH, overtime, ZERO),
            (Account.RECOGNIZED_REVENUE, ZERO, overtime - gratuity),
        ]
        if gratuity > ZERO:
            lines.append((Account.RECOGNIZED_REVENUE, ZERO, gratuity))
        ledger.post_entry(
            lead=plan.lead,
            kind=JournalEntry.Kind.OVERTIME_CAPTURED,
            lines=lines,
            idempotency_key=f"overtime-charge{charge.pk}",
            charge=charge,
            source=JournalEntry.Source.STRIPE,
            stripe_ref=payment_intent_id,
            memo="Overtime (final bill)",
        )


def _close_overtime_invoiced(lead, *, reservation_id: int | None = None) -> None:
    """Overtime is collected: a trip's charge closes that trip's task (APC-101); a historical
    final bill closed every trip's."""
    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    tasks_qs = Task.objects.filter(lead=lead, kind="overtime_invoiced", status__in=Task.UNRESOLVED)
    if reservation_id is not None:
        tasks_qs = tasks_qs.filter(reservation_id=reservation_id)
    for task in tasks_qs:
        try:
            tasks.complete(task)
        except tasks.TaskError:
            continue


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


# --- the pay link --------------------------------------------------------------------

_PAY_LINK_CHANNEL = {"sms": "phone", "email": "email"}


def send_terms_link(lead, *, base_url: str, sender_name: str) -> str:
    """APC-93 — send a staff or phone booking the link to accept the T&Cs, over Podium (SMS
    preferred, email fallback), recorded on the conversation. Returns the channel used.

    CLIENT COPY PENDING (APC-27): the body is a placeholder sentence until the wording
    document is signed off.
    """
    from apps.leads import services as lead_services

    if not outbound.enabled():
        raise PaymentError(outbound.OFF_MESSAGE)
    if lead.status != lead.Status.BOOKED:
        raise PaymentError("Only a booked order needs the terms link.")
    if lead.accepted_terms_at is not None:
        raise PaymentError("The customer has already accepted the terms.")
    link = lead_services.make_terms_page_url(lead, base_url=base_url)
    body = f"Please review and accept the terms for reservation {lead.quote_no}: {link}"
    return _send_to_customer(lead, body=body, sender_name=sender_name)


def _send_to_customer(lead, *, body: str, sender_name: str) -> str:
    """One Podium message to the order's contact, SMS preferred, email fallback, recorded
    as outbound on their conversation. Returns the channel used."""
    from apps.integrations import podium
    from apps.messaging import services as messaging_services
    from apps.messaging.models import Message

    contact = lead.contact
    if contact.phone:
        channel, identifier = "sms", contact.phone
    elif contact.email:
        channel, identifier = "email", contact.email
    else:
        raise PaymentError("No phone or email on file for this contact.")
    response = podium.send_message(
        identifier=identifier, body=body, channel_type=_PAY_LINK_CHANNEL[channel]
    )
    uid = ""
    if isinstance(response, dict):
        uid = response.get("uid") or (response.get("data") or {}).get("uid") or ""
    messaging_services.record_outbound(
        messaging_services.conversation_for(contact),
        channel=Message.Channel.SMS if channel == "sms" else Message.Channel.EMAIL,
        body=body,
        podium_message_uid=uid,
        sender_name=sender_name,
    )
    return channel


def send_pay_link(lead, *, base_url: str, sender_name: str) -> str:
    """Send the customer their pay-page link over Podium — SMS preferred, email fallback —
    and record it on the conversation. Returns the channel used. The page itself decides
    what's owed (deposit, balance or the final bill).

    Podium failures propagate as `PodiumAPIError` / `PodiumNotConnected` for the caller to
    report; a contact with no way to reach them is a `PaymentError`.
    """
    from apps.leads import services as lead_services

    if not outbound.enabled():
        raise PaymentError(outbound.OFF_MESSAGE)
    link = lead_services.make_pay_page_url(lead, base_url=base_url)
    trip = _open_trip_for(lead)
    what = f"trip {trip.reference}" if trip is not None else f"reservation {lead.quote_no}"
    body = f"Here's a secure link to pay for {what}: {link}"
    return _send_to_customer(lead, body=body, sender_name=sender_name)


def _open_trip_for(lead):
    """The trip an open pay link is for, when there is one (APC-101)."""
    plan = getattr(lead, "payment", None)
    if plan is None:
        return None
    link = (
        plan.charges.filter(kind=Charge.Kind.TRIP, status=Charge.Status.PENDING)
        .exclude(stripe_client_secret="")
        .select_related("reservation")
        .order_by("-pk")
        .first()
    )
    return link.reservation if link is not None else None
