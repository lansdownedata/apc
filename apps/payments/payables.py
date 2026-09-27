"""Affiliate payables (APC-61) — what we owe an affiliate for a farmed-out trip.

Record only: nothing here moves money. The lifecycle is

- **draft**: created when the order enters review (`ensure_payables`, from the task
  engine). `expected_amount` = the agreed payout + the affiliate's overtime, and follows
  the trip review (`refresh_for_trip`) until approval.
- **approved**: needs an invoice amount on file, and a note when it differs from what we
  expected. Accrues the *invoice* amount — D Vendor Cost / C Vendor Payable — and freezes
  `expected_amount`.
- **paid**: how and when it went out. Clears the accrual — D Vendor Payable / C Affiliate
  disbursements. Never Cash: an order's "Collected" is its Cash balance, and an affiliate
  payment isn't the customer's money going back out (Moe, 2026-09-27).

Approve and pay are gated on payments access here as well as in the views. The task
engine closes `affiliate_payable_approved` / `affiliate_paid` off the status (see
`tasks.definitions`); `tasks.sync` after each step makes that immediate.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.choices import Account

from . import ledger
from .models import AffiliatePayable, JournalEntry

ZERO = Decimal("0.00")
OPEN_STAGES = ("invoice_missing", "awaiting_approval", "approved")


class PayableError(Exception):
    """Something the payable can't accept; the message is shown to the user."""


def _confirmed_farm_outs(lead):
    from apps.dispatch.models import Assignment

    return (
        Assignment.objects.filter(
            reservation__lead=lead,
            status=Assignment.Status.CONFIRMED,
            vendor__isnull=False,
        )
        .exclude(reservation__trip_status__in=_cancelled_statuses())
        .select_related("reservation", "vendor")
    )


def _cancelled_statuses() -> list[str]:
    from apps.reservations.models import TRIP_PHASE_BY_STATUS

    return [status for status, phase in TRIP_PHASE_BY_STATUS.items() if phase == "Cancelled"]


def expected_for(assignment, review=None, *, config=None) -> Decimal:
    """Payout + the affiliate's overtime off the trip review (the override if one is set).
    With no review yet it's the payout alone."""
    from apps.reservations import reviews

    if review is None:
        review = getattr(assignment.reservation, "review", None)
    if review is None:
        return Decimal(assignment.payout or 0)
    return reviews.figures(review, assignment, config=config).expected_affiliate_amount


def ensure_payables(lead) -> list[AffiliatePayable]:
    """One payable per confirmed farmed-out trip on an order in review. Idempotent; returns
    the rows it created."""
    from apps.tasks.models import TaskConfig

    have = set(
        AffiliatePayable.objects.filter(assignment__reservation__lead=lead).values_list(
            "assignment_id", flat=True
        )
    )
    missing = [a for a in _confirmed_farm_outs(lead) if a.pk not in have]
    if not missing:
        return []
    config = TaskConfig.load()
    rows = [
        AffiliatePayable(assignment=a, expected_amount=expected_for(a, config=config))
        for a in missing
    ]
    return AffiliatePayable.objects.bulk_create(rows, ignore_conflicts=True)


def refresh_for_trip(trip) -> AffiliatePayable | None:
    """Re-read a draft payable's expected amount after its trip review changed."""
    payable = (
        AffiliatePayable.objects.filter(
            assignment__reservation=trip, status=AffiliatePayable.Status.DRAFT
        )
        .select_related("assignment__reservation", "assignment__vendor")
        .first()
    )
    if payable is None:
        return None
    expected = expected_for(payable.assignment)
    if expected != payable.expected_amount:
        payable.expected_amount = expected
        payable.save(update_fields=["expected_amount", "updated_at"])
    return payable


def _gate(user) -> None:
    if not getattr(user, "has_payments_access", False):
        raise PayableError("Only someone with payments access can do that.")


def _locked(payable: AffiliatePayable) -> AffiliatePayable:
    return AffiliatePayable.objects.select_for_update().get(pk=payable.pk)


def record_invoice(
    payable: AffiliatePayable, *, user, number: str, amount: Decimal | None, file=None
) -> AffiliatePayable:
    """What the affiliate billed. Editable until the payable is approved."""
    _gate(user)
    if amount is not None and Decimal(amount) < 0:
        raise PayableError("The invoice amount can't be negative.")
    with transaction.atomic():
        payable = _locked(payable)
        if payable.status != AffiliatePayable.Status.DRAFT:
            raise PayableError("This payable is already approved; its invoice is locked.")
        payable.invoice_number = (number or "").strip()[:64]
        payable.invoice_amount = amount
        if file is not None:
            payable.invoice_file = file
        if payable.invoice_received_at is None and (amount is not None or file is not None):
            payable.invoice_received_at = timezone.now()
        payable.save()
    refresh_for_trip(payable.assignment.reservation)
    payable.refresh_from_db()
    return payable


def approve(payable: AffiliatePayable, *, user, note: str = "") -> AffiliatePayable:
    """Freeze the expected amount and accrue the invoice amount."""
    from apps.tasks import services as tasks

    _gate(user)
    with transaction.atomic():
        payable = _locked(payable)
        if payable.status != AffiliatePayable.Status.DRAFT:
            raise PayableError("This payable is already approved.")
        if payable.invoice_amount is None:
            raise PayableError("Enter the affiliate's invoice amount before approving.")
        assignment = payable.assignment
        payable.expected_amount = expected_for(assignment)
        note = (note or "").strip()
        if payable.variance and not note:
            raise PayableError(
                "The invoice differs from what we expected. Add a note saying why before approving."
            )
        payable.status = AffiliatePayable.Status.APPROVED
        payable.approval_note = note[:255]
        payable.approved_by = user
        payable.approved_at = timezone.now()
        payable.save()
        amount = Decimal(payable.invoice_amount)
        trip = assignment.reservation
        if amount > ZERO:
            ledger.post_entry(
                lead=trip.lead,
                reservation=trip,
                kind=JournalEntry.Kind.PAYABLE_ACCRUED,
                lines=[
                    (Account.VENDOR_COST, amount, ZERO),
                    (Account.VENDOR_PAYABLE, ZERO, amount),
                ],
                idempotency_key=f"payable{payable.pk}-accrue",
                source=JournalEntry.Source.MANUAL,
                created_by=user,
                memo=f"{assignment.vendor.name} invoice {payable.invoice_number}".strip(),
            )
    tasks.sync(trip.lead)
    return payable


def mark_paid(
    payable: AffiliatePayable, *, user, method: str, reference: str = ""
) -> AffiliatePayable:
    """Record that the affiliate was paid (outside the system) and clear the accrual."""
    from apps.tasks import services as tasks

    _gate(user)
    if method not in AffiliatePayable.Method.values:
        raise PayableError("Pick how the affiliate was paid.")
    with transaction.atomic():
        payable = _locked(payable)
        if payable.status == AffiliatePayable.Status.PAID:
            raise PayableError("This payable is already marked paid.")
        if payable.status != AffiliatePayable.Status.APPROVED:
            raise PayableError("Approve the payable before marking it paid.")
        payable.status = AffiliatePayable.Status.PAID
        payable.paid_at = timezone.now()
        payable.paid_method = method
        payable.paid_reference = (reference or "").strip()[:120]
        payable.save()
        amount = Decimal(payable.invoice_amount or 0)
        trip = payable.assignment.reservation
        if amount > ZERO:
            ledger.post_entry(
                lead=trip.lead,
                reservation=trip,
                kind=JournalEntry.Kind.PAYABLE_PAID,
                lines=[
                    (Account.VENDOR_PAYABLE, amount, ZERO),
                    (Account.AFFILIATE_DISBURSEMENTS, ZERO, amount),
                ],
                idempotency_key=f"payable{payable.pk}-paid",
                source=JournalEntry.Source.MANUAL,
                created_by=user,
                memo=(
                    f"Paid by {payable.get_paid_method_display()} {payable.paid_reference}"
                ).strip(),
            )
    tasks.sync(trip.lead)
    return payable


# --- reads -----------------------------------------------------------------------


def vendor_payables(vendor, stage: str = "") -> list[AffiliatePayable]:
    """A vendor's open payables — invoice missing, awaiting approval, or approved and
    unpaid — optionally one stage only. Oldest trip first. APC-69 reuses this."""
    rows = (
        AffiliatePayable.objects.filter(assignment__vendor=vendor)
        .exclude(status=AffiliatePayable.Status.PAID)
        .select_related("assignment__reservation__lead__contact", "assignment__vendor")
        .order_by("assignment__reservation__pickup_date", "assignment__reservation__pickup_time")
    )
    wanted = (stage,) if stage in OPEN_STAGES else OPEN_STAGES
    return [p for p in rows if p.stage in wanted]


def payable_json(payable: AffiliatePayable) -> dict:
    def money(value):
        return None if value is None else f"{Decimal(value):.2f}"

    return {
        "id": payable.pk,
        "stage": payable.stage,
        "stage_label": payable.stage_label,
        "expected": money(payable.expected_amount),
        "invoice_number": payable.invoice_number,
        "invoice_amount": money(payable.invoice_amount),
        "invoice_file": payable.invoice_file.url if payable.invoice_file else "",
        "variance": money(payable.variance),
        "approval_note": payable.approval_note,
        "paid_method": payable.get_paid_method_display() if payable.paid_method else "",
        "paid_reference": payable.paid_reference,
    }
