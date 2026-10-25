"""Pricing that lives on the trip (spec 2026-10-25 §3, APC-100).

Two records describe one fact and must never disagree:

- `TripReview.billable_overtime_minutes` / `overtime_waived` — the *decision* a person made
  about overtime, with who and when (APC-59: overtime is always a judgement call).
- `Reservation.overtime_hours` — the *money*: the Overtime line in `line_total`, which the
  order page, the receipt and the invoice line all read.

`apply_review_overtime` carries a decision onto the trip; `apply_trip_overtime` carries an
edit made on the trip back onto the review, as a decision. `freeze_booked_totals` is the
booking-time snapshot processing allocates prepaid money against (spec §4.4), and
`replace_extras` is the one writer of a trip's flat extras.
"""

from __future__ import annotations

import math
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Reservation, TripExtra, TripReview

_CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def overtime_hours_from_minutes(minutes: int) -> Decimal:
    """Each started hour bills a full hour (APC-91): 90 minutes → 2.00, 60 → 1.00, 0 → 0.00."""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return ZERO
    return Decimal(math.ceil(minutes / 60)).quantize(_CENTS)


def apply_review_overtime(review: TripReview) -> None:
    """Write the review's overtime decision onto the trip's Overtime line. Called only when a
    person decided (or completed the review) — a suggestion is never money."""
    trip = review.reservation
    hours = (
        ZERO
        if review.overtime_waived
        else overtime_hours_from_minutes(review.billable_overtime_minutes)
    )
    if Decimal(trip.overtime_hours or 0) == hours:
        return
    trip.overtime_hours = hours
    trip.save(update_fields=["overtime_hours", "updated_at"])


def apply_trip_overtime(trip: Reservation, *, user=None) -> None:
    """An edit of the trip's Overtime line is a decision: write it back onto the review,
    clear any waiver and stamp who decided. A trip with no review yet (a quote) is left
    alone — never create one here."""
    review = TripReview.objects.filter(reservation=trip).first()
    if review is None:
        return
    minutes = int(Decimal(trip.overtime_hours or 0) * 60)
    if (
        review.billable_overtime_minutes == minutes
        and not review.overtime_waived
        and review.decided_at is not None
    ):
        return
    review.billable_overtime_minutes = minutes
    review.overtime_waived = False
    review.waive_reason = ""
    review.decided_by = user
    review.decided_at = timezone.now()
    review.save(
        update_fields=[
            "billable_overtime_minutes",
            "overtime_waived",
            "waive_reason",
            "decided_by",
            "decided_at",
            "updated_at",
        ]
    )


def freeze_booked_totals(lead) -> int:
    """Freeze `booked_total` on every trip of the order that has none yet. Idempotent: a
    second booking (the balance payment re-enters `book_lead`) touches nothing. Returns how
    many were frozen."""
    frozen = 0
    for trip in lead.reservations.filter(booked_total__isnull=True):
        trip.booked_total = trip.line_total
        trip.save(update_fields=["booked_total", "updated_at"])
        frozen += 1
    return frozen


@transaction.atomic
def replace_extras(trip: Reservation, rows: list[dict], *, user=None) -> list[TripExtra]:
    """Make `rows` the trip's extras. Each row: `kind`, `label`, `amount` (positive
    Decimal). Replaces wholesale — the editor posts the full list — and refreshes the
    stored `extras_total` once."""
    TripExtra.objects.filter(reservation=trip).delete()
    extras = TripExtra.objects.bulk_create(
        [
            TripExtra(
                reservation=trip,
                kind=row["kind"],
                label=row["label"],
                amount=row["amount"],
                created_by=user,
            )
            for row in rows
        ]
    )
    trip.refresh_extras_total()
    return extras
