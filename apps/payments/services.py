"""Stripe payments service — deposit authorization + balance charge, both on our own
pages. Card data never touches our servers: it is entered into Stripe's own iframes and
we only ever hold a PaymentMethod id (hosted Checkout was retired 2026-08-30)."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core import outbound
from apps.core.choices import Account
from apps.messaging import events
from apps.notifications.models import Notification
from apps.reservations.models import Reservation

from . import ledger
from .models import Charge, JournalEntry, PaymentPlan

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# How long we assume a deposit authorization stays capturable (APC-26). The card networks
# set the real ceiling — commonly ~7 days on credit, often less on debit — so this drives
# the staff countdown and the expiry sweep, never the decision to attempt a capture.
AUTH_HOLD_DAYS = 7


class PaymentError(Exception):
    """User-facing payment failure (bad amount, incomplete intent, no card)."""


ON_ACCOUNT_MESSAGE = "This order is billed to an account. Switch it back to card first."


def _card_order_only(lead) -> None:
    """An order billed to an account is invoiced, never charged (APC-105): every door that
    opens or takes card money refuses it, so no page or button can slip past."""
    if lead.on_account:
        raise PaymentError(ON_ACCOUNT_MESSAGE)


def close_open_intents(lead) -> int:
    """Cancel every card payment still open on the order (APC-105, before it is billed to an
    account): a pay page left open in a customer's tab could otherwise take card money
    after the switch. Raises `PaymentError` when one turns out to have just gone through;
    the webhook or `reconcile-payments` records it, and the switch is refused. Returns how
    many were cancelled."""
    plan = getattr(lead, "payment", None)
    if plan is None:
        return 0
    cancelled = 0
    open_charges = (
        plan.charges.filter(status=Charge.Status.PENDING)
        .exclude(stripe_payment_intent_id="")
        .order_by("pk")
    )
    for charge in open_charges:
        try:
            _stripe().PaymentIntent.cancel(charge.stripe_payment_intent_id)
        except stripe.error.StripeError as exc:
            try:
                intent = _stripe().PaymentIntent.retrieve(charge.stripe_payment_intent_id)
            except stripe.error.StripeError as unreachable:
                raise PaymentError(
                    "Stripe couldn't be reached to close an open payment. Try again."
                ) from unreachable
            if getattr(intent, "status", "") in ("succeeded", "requires_capture", "processing"):
                raise PaymentError(
                    "A card payment on this order just went through. Reload the page."
                ) from exc
            # Otherwise it is already dead at Stripe: nothing can take money through it.
        charge.status = Charge.Status.FAILED
        charge.failure_reason = "Cancelled: the order is billed to an account"
        charge.save(update_fields=["status", "failure_reason", "updated_at"])
        cancelled += 1
    return cancelled


def _stripe():
    # None, not "": stripe-python sends a request with an empty key and lets the 401 refuse it.
    stripe.api_key = settings.STRIPE_SECRET_KEY or None
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
    Charge to SUCCEEDED), the one place that cancels deposit/balance intents
    (`close_open_intents`) marks their rows FAILED as it goes, and a genuinely dead one
    only makes `confirmPayment` error client-side, which a refresh clears. That is worth not
    paying a Stripe round-trip on every hit of a public endpoint.

    A changed amount means a partial payment landed between page loads: a fresh Charge (and so
    a fresh idempotency key) is created and the old PENDING one is abandoned, not cancelled —
    Stripe expires unconfirmed intents on its own.
    """
    _card_order_only(plan.lead)
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
    _card_order_only(plan.lead)
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
                "lead_id": str(plan.lead_id),
                "kind": Charge.Kind.BALANCE.value,
                "charge_id": str(charge.pk),
            },
            idempotency_key=charge.idempotency_key,
        )
    except (stripe.error.CardError, stripe.error.InvalidRequestError) as exc:
        # InvalidRequestError: the saved card is gone (detached) — a failed balance the
        # office must see, not an unhandled error that leaves a PENDING row (APC-47).
        return _record_failure(plan, charge, exc)

    charge.stripe_payment_intent_id = intent.id
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    # Settled through the one reconcile every path shares (APC-126): the webhook for this
    # same intent may already have finished the row, and then this is a no-op.
    return record_payment(plan, intent.id, intent=intent, balance_settled=True)


# Newest money back first (Moe, 2026-10-24): the trip's own charges (APC-101), then a
# cleaning hold captured for it (APC-95), then a historical final bill, then the balance,
# then the deposit.
_REFUND_ORDER = (
    Charge.Kind.TRIP,
    Charge.Kind.HOLD,
    Charge.Kind.FINAL,
    Charge.Kind.BALANCE,
    Charge.Kind.DEPOSIT,
)


def _refunded_on(plan, payment_intent_id: str) -> Decimal:
    from django.db.models import Sum

    return plan.charges.filter(
        kind=Charge.Kind.REFUND, stripe_payment_intent_id=payment_intent_id
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")


def _refundable_sources(plan, *, trip=None) -> list[tuple[Charge, Decimal]]:
    """(charge, still refundable) for every succeeded Stripe charge, in refund order. With
    `trip`, only that trip's own money counts — its TRIP charges and a cleaning hold
    captured for it (another trip's money is not this trip's to return); without, every
    one counts, newest first. Only a captured hold is SUCCEEDED: one still on the card is
    not money."""
    rank = {kind: i for i, kind in enumerate(_REFUND_ORDER)}
    charges = plan.charges.filter(kind__in=_REFUND_ORDER, status=Charge.Status.SUCCEEDED)
    if trip is not None:
        charges = charges.exclude(kind__in=TRIP_MONEY_KINDS) | charges.filter(
            kind__in=TRIP_MONEY_KINDS, reservation=trip
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
        events.schedule_dates(plan.lead)
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


def _snapshot_plan_card(plan: PaymentPlan, charge: Charge) -> None:
    """An off-session charge paid with the card on the plan: that card is what paid it
    (APC-40). Nothing is written when the plan knows no card, and a snapshot already on
    the row — the webhook resolved it from Stripe first — is left alone."""
    if charge.card_brand or charge.card_last4 or not (plan.card_brand or plan.card_last4):
        return
    charge.card_brand = plan.card_brand
    charge.card_last4 = plan.card_last4
    charge.save(update_fields=["card_brand", "card_last4", "updated_at"])


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


# A charge of these kinds newly succeeding is a message event (spec C section 3).
_PAID_EVENTS = {Charge.Kind.DEPOSIT: "deposit_paid", Charge.Kind.BALANCE: "balance_paid"}

# Money for one trip: a trip charge, or a cleaning hold captured for a trip (APC-95), which
# posts, recognises and receipts exactly as a trip charge does.
TRIP_MONEY_KINDS = (Charge.Kind.TRIP, Charge.Kind.HOLD)


def _metadata(intent) -> dict:
    """Stripe objects are dict-like; a test double may not be."""
    metadata = getattr(intent, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _charge_for(plan: PaymentPlan, intent, payment_intent_id: str, kind: str) -> Charge:
    """The Charge this intent pays, row-locked for the caller's transaction.

    By intent id first. Failing that, by the `charge_id` every path of ours puts in the
    intent's metadata — the only key that exists while an off-session charge is still
    between Stripe's reply and its own save, which is exactly when Stripe can deliver
    `succeeded` (APC-126). The lock is what makes the two writers take turns instead of
    each finishing its own copy. A row is minted only for an intent that carries neither,
    which nothing of ours creates today.
    """
    locked = plan.charges.select_for_update()
    charge = locked.filter(stripe_payment_intent_id=payment_intent_id).first()
    if charge is None:
        charge_id = _metadata(intent).get("charge_id")
        if charge_id:
            charge = locked.filter(
                pk=charge_id, stripe_payment_intent_id__in=("", payment_intent_id)
            ).first()
    if charge is None:
        amount = (Decimal(intent.amount) / Decimal(100)).quantize(Decimal("0.01"))
        reservation_id = _metadata(intent).get("reservation_id")
        charge = plan.record_charge(
            kind=kind,
            amount=amount,
            reservation_id=int(reservation_id) if reservation_id else None,
        )
    if charge.stripe_payment_intent_id != payment_intent_id:
        charge.stripe_payment_intent_id = payment_intent_id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return charge


def _memo(charge: Charge) -> str:
    """What the ledger entry for a newly succeeded charge says."""
    if charge.kind == Charge.Kind.DEPOSIT:
        return "Deposit captured"
    if charge.kind == Charge.Kind.HOLD:
        if charge.reservation_id:
            return f"Cleaning fee from the hold ({charge.reservation})"
        return "Cleaning hold captured"
    if charge.kind == Charge.Kind.TRIP:
        return f"Trip charge ({charge.reservation})"
    return "Card payment"


def _settle(
    plan: PaymentPlan, charge: Charge, payment_intent_id: str, *, memo: str | None = None
) -> tuple[str | None, bool]:
    """Flip a locked charge to SUCCEEDED and post its money, once. Returns the message event
    a newly succeeded deposit or balance fires (else None) and whether a trip receipt is now
    due. An already-succeeded row — the other writer got here first — changes nothing."""
    if charge.status == Charge.Status.SUCCEEDED:
        return None, False
    if charge.kind == Charge.Kind.FINAL:
        charge.status = Charge.Status.SUCCEEDED
        charge.save(update_fields=["status", "updated_at"])
        _post_final(plan, charge, payment_intent_id)
        return None, False
    is_deposit = charge.kind == Charge.Kind.DEPOSIT
    is_trip = charge.kind in TRIP_MONEY_KINDS
    receipt_now = False
    charge.status = Charge.Status.SUCCEEDED
    # A trip charge is processing's money: its receipt is due from now (APC-103).
    if is_trip and charge.reservation_id and charge.receipt_due_at is None:
        charge.receipt_due_at = timezone.now()
        receipt_now = True
    charge.save(update_fields=["status", "receipt_due_at", "updated_at"])
    ledger.post_capture(
        lead=plan.lead,
        amount=charge.amount,
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED
        if is_deposit
        else JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key=f"capture-charge{charge.pk}",
        charge=charge,
        stripe_ref=payment_intent_id,
        memo=memo or _memo(charge),
        reservation=charge.reservation if is_trip else None,
    )
    return _PAID_EVENTS.get(charge.kind), receipt_now


def record_payment(
    plan: PaymentPlan,
    payment_intent_id: str,
    *,
    kind: str = Charge.Kind.BALANCE,
    intent=None,
    balance_settled: bool = False,
) -> Charge:
    """Reconcile a succeeded PaymentIntent: ledger, card, statuses, maybe book.

    The single reconcile for every surface — the customer pay page, the staff Payment
    Element, the webhook, the reconcile sweep and the off-session charges. `kind` selects
    the Charge kind and the matching journal entry; everything else is identical, which is
    the point. The row is found and settled under a lock (`_charge_for`, `_settle`), so two
    of these for one intent — the inline caller and the webhook — take turns, and the
    second changes nothing (APC-126).

    The `expand=["payment_method"]` retrieve is load-bearing, not redundant: webhook payloads
    carry `payment_method` as a bare ID (expanded payloads are opt-in and we don't use them),
    so this is what resolves the card on every path. An off-session caller passes the
    `intent` it confirmed instead: it charged the card on the plan by definition, so that
    card is snapshotted onto the charge with no Stripe round-trip. `balance_settled` is
    the balance cron's contract: its amount was clamped to what is still owed, so the plan's
    balance is PAID even when the live total has drifted under the snapshot
    `sync_plan_from_collected` measures against — and it is set before the tasks and the
    balance-paid message read the order.

    Plan flags are left to `sync_plan_from_collected` — it already sets deposit PAID +
    balance SCHEDULED once collected covers the deposit, so a deposit needs no special case.
    """
    from apps.leads.models import Lead
    from apps.leads.services import book_lead

    if intent is None:
        intent = _stripe().PaymentIntent.retrieve(payment_intent_id, expand=["payment_method"])
        payment_method = getattr(intent, "payment_method", None)
    else:
        payment_method = None
    if intent.status != "succeeded":
        raise PaymentError(f"Payment has not succeeded ({intent.status}).")

    with transaction.atomic():
        charge = _charge_for(plan, intent, payment_intent_id, kind)
        if (
            charge.kind == Charge.Kind.HOLD
            and charge.reservation_id is None
            and charge.status != Charge.Status.SUCCEEDED
        ):
            # A cleaning hold is booked by its capture for a trip (`holds.capture_hold`),
            # never as order money for the whole authorization.
            raise PaymentError("A cleaning hold is booked only when captured for a trip.")
        paid_event, receipt_now = _settle(
            plan, charge, payment_intent_id, memo="Balance captured" if balance_settled else None
        )
        if balance_settled and plan.balance_status != PaymentPlan.BalanceStatus.PAID:
            plan.balance_status = PaymentPlan.BalanceStatus.PAID
            plan.save(update_fields=["balance_status", "updated_at"])

    if payment_method is not None:
        _store_card(plan, payment_method, charge)
    else:
        _snapshot_plan_card(plan, charge)
    sync_plan_from_collected(plan)
    if charge.kind == Charge.Kind.FINAL:
        _close_overtime_invoiced(plan.lead)
    elif charge.kind in TRIP_MONEY_KINDS and charge.reservation_id:
        # A captured cleaning hold pays the cleaning fee only: the trip's overtime is still
        # to collect, so only a trip charge closes its task.
        if charge.kind == Charge.Kind.TRIP:
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
    if paid_event:
        # After the booking and the plan's flags: a message due now goes out on commit,
        # which outside a transaction is right here, and must read the order as it stands.
        events.emit(paid_event, plan.lead)
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
        events.cancel(lead)
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
    _card_order_only(lead)
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
    # After book_lead, so the confirmation isn't swept up by its cancel.
    lead.refresh_from_db()
    events.emit("order_confirmed", lead)
    return captured


def cancel_order(lead, *, user=None, reason: str = "") -> Charge:
    """APC could not cover the trip — release the hold and lose the lead (APC-26).

    `PaymentIntent.cancel()` returns the held funds; no money ever moved, so there is
    nothing to refund and nothing for the ledger to say.
    """
    from apps.leads.models import Lead

    _card_order_only(lead)
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
    # The order is lost with its trips still on it, so any cleaning hold goes regardless.
    _release_cleaning_hold(lead)
    events.cancel(lead, triggers=events.EVERY)
    # Queued *after* the sweep above, which cancels every pending message — the message
    # explaining the cancellation must not be cancelled by the cancellation.
    events.emit("order_cancelled", lead)
    return charge


# --- cancel & refund by trip (APC-127) ---------------------------------------------------


def _live_trips(lead) -> list:
    """The order's trips not yet cancelled, in pickup order."""
    from apps.dispatch.selectors import CANCELLED_STATUSES

    trips = [r for r in lead.reservations.all() if r.trip_status not in CANCELLED_STATUSES]
    return sorted(trips, key=lambda t: (t.pickup_date is None, t.pickup_date, t.pickup_time, t.pk))


def cancel_shares(lead) -> dict[int, Decimal]:
    """What comes back to the card for each live trip if it alone is cancelled: the
    refundable money on its own charges, plus the order-level money (deposit, balance)
    split across the live trips in proportion to their prices, rounded down to cents with
    the remainder on the last trip by pickup, so the shares add up to everything refundable
    (`refundable_total`, net of money paid back off-card)."""
    from collections import defaultdict
    from decimal import ROUND_DOWN

    cents = Decimal("0.01")
    live = _live_trips(lead)
    plan = getattr(lead, "payment", None)
    if not live:
        return {}
    if plan is None:
        return {t.pk: Decimal("0.00") for t in live}
    total = refundable_total(plan)
    own: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    live_ids = {t.pk for t in live}
    for src, left in _refundable_sources(plan):
        if src.reservation_id in live_ids:
            own[src.reservation_id] += left
    pool = max(total - sum(own.values(), Decimal("0.00")), Decimal("0.00"))
    weights = {t.pk: t.line_total for t in live}
    weight_sum = sum(weights.values(), Decimal("0.00"))
    shares: dict[int, Decimal] = {}
    for trip in live[:-1]:
        slice_ = pool * weights[trip.pk] / weight_sum if weight_sum else pool / len(live)
        shares[trip.pk] = (own[trip.pk] + slice_).quantize(cents, rounding=ROUND_DOWN)
    last = live[-1]
    shares[last.pk] = max(total - sum(shares.values(), Decimal("0.00")), Decimal("0.00"))
    return shares


def cancel_options(lead, user) -> list[dict]:
    """The Cancel & refund dialog's rows: each live trip, what its cancellation refunds,
    and whether this user may cancel it (a trip whose pickup has passed takes an Admin)."""
    from apps.core.templatetags.time_filters import pickup_clock

    shares = cancel_shares(lead)
    now = timezone.now()
    rows = []
    for trip in _live_trips(lead):
        pickup = trip.pickup_at
        past = pickup is not None and pickup <= now
        when = f"{trip.pickup_date:%a, %b %-d, %Y}" if trip.pickup_date else "No date yet"
        if trip.pickup_time is not None:
            when = f"{when} at {pickup_clock(trip)}"
        rows.append(
            {
                "pk": trip.pk,
                "reference": trip.reference,
                "when": when,
                "amount": f"{shares[trip.pk]:.2f}",
                "past": past,
                "allowed": not past or user.is_owner_admin,
            }
        )
    return rows


@dataclass
class CancelResult:
    cancelled: list
    refunded: Decimal
    order_cancelled: bool


def cancel_trips(lead, trips, *, user) -> CancelResult:
    """Cancel the chosen trips of a booked order and refund each one's share (APC-127).

    Every live trip chosen is today's whole-order cancellation: everything refundable comes
    back in one refund and the order is lost. Otherwise each trip's share (`cancel_shares`)
    is refunded against that trip, and the order stays booked with the rest. A trip whose
    pickup has passed takes an Admin. A refund Stripe refuses raises before anything is
    cancelled (what did refund is on the books), as the order console always has."""
    from apps.leads.models import Lead

    if lead.status != Lead.Status.BOOKED:
        raise PaymentError("Only booked orders can be cancelled.")
    live = _live_trips(lead)
    wanted = {t.pk for t in trips}
    if not wanted:
        raise PaymentError("Pick at least one trip to cancel.")
    chosen = [t for t in live if t.pk in wanted]
    if len(chosen) != len(wanted):
        raise PaymentError("That trip isn't on this order, or is already cancelled.")
    now = timezone.now()
    if not user.is_owner_admin and any(
        t.pickup_at is not None and t.pickup_at <= now for t in chosen
    ):
        raise PaymentError("Only an Admin can cancel a trip whose pickup has passed.")
    whole = len(chosen) == len(live)
    plan = getattr(lead, "payment", None)
    refunded = Decimal("0.00")
    if plan is not None:
        if whole:
            refunded = refund_payment(plan, refundable_total(plan))
        else:
            shares = cancel_shares(lead)
            for trip in chosen:
                if shares[trip.pk] > Decimal("0.00"):
                    refunded += refund_payment(plan, shares[trip.pk], reservation=trip)
    _cancel(lead, chosen, whole=whole)
    return CancelResult(cancelled=chosen, refunded=refunded, order_cancelled=whole)


def _cancel(lead, trips, *, whole: bool) -> None:
    """Cancel these trips once their money is back: status Cancelled with revenue
    reversed, any coverage let go, their queued messages withdrawn, `trip_cancelled` each.
    ``whole`` (no live trip left) also loses the order, withdraws every queued message
    and emits `order_cancelled`, so the seeded Order cancelled message goes out.

    The events come last, after every write, because a message due now goes out on
    commit, which outside a transaction is right here."""
    from apps.dispatch import services as dispatch_services
    from apps.leads.models import Lead
    from apps.reservations.models import Reservation

    pks = [t.pk for t in trips]
    for trip in trips:
        trip.trip_status = Reservation.TripStatus.CANCELLED
        trip.revenue_status = Reservation.RevenueStatus.REVERSED
        trip.save(update_fields=["trip_status", "revenue_status", "updated_at"])
    _report_cancels_to_gnet(trips)
    # Cancelled trips drop off the dispatch board, so any affiliate offer still open on
    # them would be unreachable — pull it here while we still know the trip died.
    dispatch_services.release_trips(
        lead.reservations.all() if whole else lead.reservations.filter(pk__in=pks),
        note="Order cancelled" if whole else "Trip cancelled",
    )
    from . import holds

    if not holds.held_trips(lead):
        _release_cleaning_hold(lead)
    if whole:
        lead.status = Lead.Status.LOST
        lead.lost_reason = "Cancelled"
        lead.has_alert = False
        lead.save(update_fields=["status", "lost_reason", "has_alert", "updated_at"])
        events.cancel(lead, triggers=events.EVERY)
    else:
        for trip in trips:
            events.cancel(lead, triggers=events.EVERY, reservation=trip)
    for trip in Reservation.objects.filter(pk__in=pks).select_related("lead"):
        events.emit("trip_cancelled", lead, trip)
    if whole:
        events.emit("order_cancelled", lead)


def _release_cleaning_hold(lead) -> None:
    """Let the order's cleaning hold go (APC-95): nothing is left for it to cover. A release
    Stripe refuses is logged, never raised, so it can't undo a cancel whose money is already
    back; the hourly `cleaning-holds` sweep releases it on its next run."""
    from . import holds

    try:
        holds.release_hold(lead, reason="order cancelled")
    except PaymentError:
        logger.exception("cleaning hold: release for lead %s failed on cancel", lead.pk)


def _report_cancels_to_gnet(trips) -> None:
    """Queue GNet's CANCEL for each farmed-in trip: the per-trip save above skips
    `set_trip_status`, which is where that hook normally runs. A failure is logged, never
    raised, so it can't undo a cancel whose money is already back."""
    from apps.dispatch import farm_in_push
    from apps.reservations.models import Reservation

    for trip in trips:
        try:
            farm_in_push.after_trip_status(trip, Reservation.TripStatus.CANCELLED)
        except Exception:
            logger.exception("farm-in: CANCEL push for trip %s failed to queue", trip.pk)


def cancel_booked_order(lead) -> list:
    """Cancel a booked order once its money is back: every live trip, no permission
    check (the console's whole-order path and the tests). Returns the trips cancelled."""
    live = _live_trips(lead)
    if live:
        _cancel(lead, live, whole=True)
    return live


def expire_authorization(charge: Charge) -> Charge:
    """The issuer released a hold before APC confirmed the order (APC-26, Exception 1).

    Nobody is at fault and no money moved, so this is a *reversal of state, not of money*:
    no ledger entry, no refund. The order stops being engaged and becomes a live quote
    again, because the one thing the customer needs is a working way to book it a second
    time — with an explanation, which the deposit-hold-released message carries.
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
        events.emit("deposit_hold_released", lead)

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
    _card_order_only(plan.lead)
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


def send_terms_link(lead, *, base_url: str, sender_name: str) -> str:
    """APC-93 — send a staff or phone booking the link to accept the T&Cs, over Podium (SMS
    preferred, email fallback), recorded on the conversation. Returns the channel used.
    The wording is the "Terms link" message row's (CLIENT COPY PENDING, APC-27).
    """
    from apps.leads import services as lead_services

    if not outbound.enabled():
        raise PaymentError(outbound.OFF_MESSAGE)
    if lead.status != lead.Status.BOOKED:
        raise PaymentError("Only a booked order needs the terms link.")
    if lead.accepted_terms_at is not None:
        raise PaymentError("The customer has already accepted the terms.")
    link = lead_services.make_terms_page_url(lead, base_url=base_url)
    return _send_link(lead, "send_terms_link", {"terms_link": link}, sender_name=sender_name)


def _reach(key: str, lead):
    """Who a link goes to (the row's recipients on this order, APC-129) and the channel its
    first person gets it on: text when they have a mobile, email otherwise."""
    from apps.messaging import sender

    people = sender.resolve_for(key, lead)
    for person in people:
        if person.phone and "sms" in person.channels:
            return "sms", person, people
        if person.email and "email" in person.channels:
            return "email", person, people
    raise PaymentError("No phone or email on file for this contact.")


def _send_link(lead, key: str, values: dict, *, sender_name: str) -> str:
    """Send message row ``key`` (a link) to the people it lists; returns the channel the
    first of them got it on."""
    from apps.messaging import sender

    channel, person, _ = _reach(key, lead)
    body = sender.podium_text(key, lead, person, channel, extra_ctx=values)
    return _send_to_customer(
        lead, key=key, body=body, values=values, channel=channel, sender_name=sender_name
    )


def _send_to_customer(lead, *, key, body, values, channel, sender_name) -> str:
    """One Podium message (the row ``key``, which renders as ``body``) to each person the
    row lists, texted when they have a mobile and emailed otherwise, recorded as outbound
    on the order contact's conversation. Returns ``channel``, the first person's.

    A send that fails raises `PodiumAPIError` with the provider's reason, for the caller to
    report as the gateway failure it is."""
    from apps.integrations.podium import PodiumAPIError
    from apps.messaging import sender
    from apps.messaging import services as messaging_services
    from apps.messaging.models import Delivery, Message

    result = sender.send_now(key, lead, extra_ctx=values, text_first=True)
    if not result.sent:
        raise PodiumAPIError("; ".join(result.errors) or "The message was not sent.")
    uid = next((d.provider_id for d in result.deliveries if d.status == Delivery.Status.SENT), "")
    messaging_services.record_outbound(
        messaging_services.conversation_for(lead.contact),
        channel=Message.Channel.SMS if channel == "sms" else Message.Channel.EMAIL,
        body=body,
        podium_message_uid=uid,
        sender_name=sender_name,
    )
    return channel


def send_pay_link(lead, *, base_url: str, sender_name: str) -> str:
    """Send the customer their pay-page link over Podium — SMS preferred, email fallback —
    and record it on the conversation. Returns the channel used. The page itself decides
    what's owed (deposit, balance or the final bill); the wording is the "Payment link"
    message row's.

    A Podium failure raises `PodiumAPIError` for the caller to report; a contact with no way
    to reach them is a `PaymentError`.
    """
    _card_order_only(lead)
    from apps.leads import services as lead_services

    if not outbound.enabled():
        raise PaymentError(outbound.OFF_MESSAGE)
    link = lead_services.make_pay_page_url(lead, base_url=base_url)
    trip = _open_trip_for(lead)
    what = f"trip {trip.reference}" if trip is not None else f"reservation {lead.quote_no}"
    values = {"pay_link": link, "payment_for": what}
    return _send_link(lead, "send_pay_link", values, sender_name=sender_name)


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
