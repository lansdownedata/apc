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


def post_capture(*, lead, amount, kind, idempotency_key, charge=None,
                 source=JournalEntry.Source.STRIPE, memo=""):
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
        lead=lead, kind=kind, lines=lines, idempotency_key=idempotency_key,
        charge=charge, source=source, memo=memo,
    )


def recognize_reservation(reservation):
    """Recognize one trip's revenue: draw deferred first, overflow to A/R. Idempotent."""
    from apps.reservations.models import Reservation

    lead = reservation.lead
    amount = reservation.line_total
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
