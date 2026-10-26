"""Double-entry posting service — the only writer of the ledger (spec §3–§4)."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.choices import Account

from .models import JournalEntry, JournalLine

ZERO = Decimal("0.00")


class LedgerError(Exception):
    """Raised when an entry would be unbalanced."""


def post_entry(
    *,
    lead,
    kind,
    lines,
    idempotency_key,
    reservation=None,
    charge=None,
    source=JournalEntry.Source.SYSTEM,
    memo="",
    created_by=None,
    stripe_ref="",
):
    """Post one balanced entry. `lines` = list of (account, debit, credit). Idempotent."""
    existing = JournalEntry.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing

    total_debit = sum((debit for _, debit, _ in lines), ZERO)
    total_credit = sum((credit for _, _, credit in lines), ZERO)
    if total_debit != total_credit:
        raise LedgerError(
            f"Unbalanced entry {idempotency_key!r}: debit {total_debit} != credit {total_credit}"
        )

    try:
        with transaction.atomic():
            entry = JournalEntry.objects.create(
                lead=lead,
                reservation=reservation,
                kind=kind,
                source=source,
                memo=memo,
                charge=charge,
                created_by=created_by,
                stripe_ref=stripe_ref,
                idempotency_key=idempotency_key,
            )
            JournalLine.objects.bulk_create(
                [
                    JournalLine(entry=entry, account=account, debit=debit, credit=credit)
                    for account, debit, credit in lines
                ]
            )
    except IntegrityError:
        # Lost a race on idempotency_key — return the entry that won.
        return JournalEntry.objects.get(idempotency_key=idempotency_key)
    return entry


def account_balance(lead, account) -> Decimal:
    """Σ debit − Σ credit for one account on one order."""
    agg = JournalLine.objects.filter(entry__lead=lead, account=account).aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def order_balances(lead) -> dict:
    """Business-meaningful positive balances for an order."""
    return {
        "collected": account_balance(lead, Account.CASH),
        "deferred": -account_balance(lead, Account.CUSTOMER_DEPOSITS),
        "ar": account_balance(lead, Account.ACCOUNTS_RECEIVABLE),
        "recognized": -account_balance(lead, Account.RECOGNIZED_REVENUE),
        "refunded": account_balance(lead, Account.REFUNDS),
    }


def post_capture(
    *,
    lead,
    amount,
    kind,
    idempotency_key,
    charge=None,
    source=JournalEntry.Source.STRIPE,
    stripe_ref="",
    memo="",
    reservation=None,
):
    """Cash in. Clears any outstanding A/R first, then adds to deferred revenue."""
    amount = Decimal(amount)
    to_ar = min(amount, order_balances(lead)["ar"])
    to_deferred = amount - to_ar
    lines = [(Account.CASH, amount, ZERO)]
    if to_ar > ZERO:
        lines.append((Account.ACCOUNTS_RECEIVABLE, ZERO, to_ar))
    if to_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, ZERO, to_deferred))
    return post_entry(
        lead=lead,
        kind=kind,
        lines=lines,
        idempotency_key=idempotency_key,
        charge=charge,
        source=source,
        stripe_ref=stripe_ref,
        memo=memo,
        reservation=reservation,
    )


def post_refund(*, lead, amount, idempotency_key, charge=None, created_by=None, memo="Refund"):
    """Cash out. Reverses deferred first; any excess reverses recognized revenue (Refunds)."""
    amount = Decimal(amount)
    deferred = order_balances(lead)["deferred"]
    from_deferred = min(amount, deferred) if deferred > ZERO else ZERO
    from_refunds = amount - from_deferred
    lines = [(Account.CASH, ZERO, amount)]
    if from_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, from_deferred, ZERO))
    if from_refunds > ZERO:
        lines.append((Account.REFUNDS, from_refunds, ZERO))
    return post_entry(
        lead=lead,
        kind=JournalEntry.Kind.REFUND_ISSUED,
        lines=lines,
        idempotency_key=idempotency_key,
        charge=charge,
        created_by=created_by,
        source=JournalEntry.Source.MANUAL,
        memo=memo,
    )


def post_trip_refund(
    *, lead, reservation, amount, idempotency_key, charge=None, created_by=None, memo="Refund"
):
    """Cash out for a trip whose revenue is already recognised (APC-101): straight to
    Refunds, the contra account every refund of earned money uses. Deferred revenue is
    untouched — that is other trips' money, not this one's."""
    amount = Decimal(amount)
    return post_entry(
        lead=lead,
        reservation=reservation,
        kind=JournalEntry.Kind.REFUND_ISSUED,
        lines=[(Account.CASH, ZERO, amount), (Account.REFUNDS, amount, ZERO)],
        idempotency_key=idempotency_key,
        charge=charge,
        created_by=created_by,
        source=JournalEntry.Source.MANUAL,
        memo=memo,
    )


def post_overtime_refund(*, lead, base, gratuity, idempotency_key, charge=None, created_by=None):
    """Cash out against a final bill's overtime (APC-75). Books to Refunds, the contra
    account every refund of earned money uses (Moe, 2026-10-24), base and gratuity as two
    lines — so "total refunded" includes it and Recognized keeps what was earned. Deferred
    revenue is untouched: the overtime was never deferred."""
    base, gratuity = Decimal(base), Decimal(gratuity)
    lines = [(Account.CASH, ZERO, base + gratuity)]
    if base > ZERO:
        lines.append((Account.REFUNDS, base, ZERO))
    if gratuity > ZERO:
        lines.append((Account.REFUNDS, gratuity, ZERO))
    return post_entry(
        lead=lead,
        kind=JournalEntry.Kind.REFUND_ISSUED,
        lines=lines,
        idempotency_key=idempotency_key,
        charge=charge,
        created_by=created_by,
        source=JournalEntry.Source.MANUAL,
        memo="Refund (overtime)",
    )


def post_forfeit(*, lead, amount, idempotency_key, created_by=None, memo="Deposit forfeited"):
    """Cancellation: reclassify deferred cash to cancellation revenue. No cash moves."""
    amount = Decimal(amount)
    lines = [
        (Account.CUSTOMER_DEPOSITS, amount, ZERO),
        (Account.CANCELLATION_REVENUE, ZERO, amount),
    ]
    return post_entry(
        lead=lead,
        kind=JournalEntry.Kind.DEPOSIT_FORFEITED,
        lines=lines,
        idempotency_key=idempotency_key,
        created_by=created_by,
        source=JournalEntry.Source.MANUAL,
        memo=memo,
    )


def recognize_reservation(reservation):
    """Recognize one trip's revenue: draw deferred first, overflow to A/R. Idempotent."""
    from apps.reservations.models import Reservation

    lead = reservation.lead
    # A historical final bill (APC-60) credited this trip's overtime to revenue at capture;
    # the back-fill then put the hours on the trip. Don't book that part twice.
    amount = reservation.line_total - _final_overtime_booked_for(reservation)
    if amount <= ZERO:
        return None  # nothing to recognize (e.g. a comped $0 trip) — post no entry
    deferred = order_balances(lead)["deferred"]
    from_deferred = min(amount, deferred) if deferred > ZERO else ZERO
    to_ar = amount - from_deferred

    lines = [(Account.RECOGNIZED_REVENUE, ZERO, amount)]
    if from_deferred > ZERO:
        lines.append((Account.CUSTOMER_DEPOSITS, from_deferred, ZERO))
    if to_ar > ZERO:
        lines.append((Account.ACCOUNTS_RECEIVABLE, to_ar, ZERO))

    entry = post_entry(
        lead=lead,
        reservation=reservation,
        kind=JournalEntry.Kind.REVENUE_RECOGNIZED,
        lines=lines,
        idempotency_key=f"recognize-res{reservation.pk}",
        memo=f"Recognize {reservation}",
    )
    if reservation.revenue_status != Reservation.RevenueStatus.RECOGNIZED:
        reservation.revenue_status = Reservation.RevenueStatus.RECOGNIZED
        reservation.recognized_at = timezone.now()
        reservation.recognized_amount = amount
        reservation.save(
            update_fields=["revenue_status", "recognized_at", "recognized_amount", "updated_at"]
        )
    return entry


def _final_overtime_booked_for(reservation) -> Decimal:
    """The overtime a historical FINAL charge (APC-60) already credited to revenue for this
    trip: its overtime lines, when the order paid a final bill and the trip's overtime was
    back-filled from it. Nothing creates FINAL charges since APC-101."""
    from .models import Charge

    paid_final = Charge.objects.filter(
        plan__lead=reservation.lead, kind=Charge.Kind.FINAL, status=Charge.Status.SUCCEEDED
    ).exists()
    if not paid_final:
        return ZERO
    return reservation.overtime_amount + reservation.overtime_gratuity


def effective_recognized(reservation) -> Decimal:
    """What this trip's revenue already stands at: what was recognised, less the refunds
    made for the trip (each posted contra-revenue when it happened), plus any overtime a
    historical final bill booked at capture."""
    from .models import Charge

    refunds = Charge.objects.filter(
        reservation=reservation, kind=Charge.Kind.REFUND, status=Charge.Status.SUCCEEDED
    )
    if reservation.recognized_at is not None:
        # A refund made before recognition came out of deferred cash, not revenue; the
        # recognition that followed already read the credit line it left on the trip.
        refunds = refunds.filter(created_at__gte=reservation.recognized_at)
    refunded = refunds.aggregate(s=Sum("amount"))["s"] or ZERO
    recognized = Decimal(reservation.recognized_amount or 0)
    return (recognized - Decimal(refunded) + _final_overtime_booked_for(reservation)).quantize(
        Decimal("0.01")
    )


def _adjustments(reservation) -> int:
    return JournalEntry.objects.filter(
        reservation=reservation, kind=JournalEntry.Kind.ADJUSTMENT
    ).count()


def true_up_recognition(reservation):
    """Processing's recognition step (spec 2026-10-25 §4.4): bring the trip's recognised
    revenue to its live `line_total`.

    A trip that is not earned (not Done, No Show or Late Cancel) is left alone. One not yet
    recognised is recognised now. Otherwise the difference against `effective_recognized`
    posts as an ADJUSTMENT: more revenue draws deferred cash first then A/R (recognition's
    rule); less revenue gives A/R back first, then deferred (an overpayment the customer
    can be refunded). Idempotent at the same total."""
    from apps.reservations.models import PROCESSED_EARNED_STATUSES, Reservation

    if reservation.trip_status not in PROCESSED_EARNED_STATUSES:
        return None
    if reservation.revenue_status != Reservation.RevenueStatus.RECOGNIZED:
        return recognize_reservation(reservation)
    lead = reservation.lead
    target = reservation.line_total
    delta = (target - effective_recognized(reservation)).quantize(Decimal("0.01"))
    if delta == ZERO:
        return None
    balances = order_balances(lead)
    if delta > ZERO:
        from_deferred = min(delta, balances["deferred"]) if balances["deferred"] > ZERO else ZERO
        lines = [(Account.RECOGNIZED_REVENUE, ZERO, delta)]
        if from_deferred > ZERO:
            lines.append((Account.CUSTOMER_DEPOSITS, from_deferred, ZERO))
        if delta - from_deferred > ZERO:
            lines.append((Account.ACCOUNTS_RECEIVABLE, delta - from_deferred, ZERO))
    else:
        back = -delta
        to_ar = min(back, balances["ar"]) if balances["ar"] > ZERO else ZERO
        lines = [(Account.RECOGNIZED_REVENUE, back, ZERO)]
        if to_ar > ZERO:
            lines.append((Account.ACCOUNTS_RECEIVABLE, ZERO, to_ar))
        if back - to_ar > ZERO:
            lines.append((Account.CUSTOMER_DEPOSITS, ZERO, back - to_ar))
    entry = post_entry(
        lead=lead,
        reservation=reservation,
        kind=JournalEntry.Kind.ADJUSTMENT,
        lines=lines,
        idempotency_key=f"recognize-res{reservation.pk}-adj{_adjustments(reservation) + 1}",
        memo=f"Revenue true-up {reservation}",
    )
    reservation.recognized_amount = Decimal(reservation.recognized_amount or 0) + delta
    reservation.save(update_fields=["recognized_amount", "updated_at"])
    return entry
