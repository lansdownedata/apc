"""Back-fill for pricing-on-the-trip (spec 2026-10-25 §3.3, APC-100).

1. Every trip on a BOOKED order gets `booked_total` = its line total as it stood before any
   overtime moved onto the trip (the freeze a booking would have done).
2. Orders that already paid a FINAL charge (APC-60, one per order) get that overtime written
   onto the trips it came from: each completed, non-waived review's billable minutes become
   `overtime_hours`, as exact hours (not rounded up) so the trip totals reproduce what was
   charged. An order whose split does not add up to the cent is logged and left alone
   (`overtime_hours` stays 0 there), per spec §3.3.

The money formulas are copied here on purpose: a migration cannot read model properties.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

logger = logging.getLogger("apps.reservations.migrations")

CENTS = Decimal("0.01")
BOOKED = "booked"


def _line_total(trip) -> Decimal:
    """`Reservation.line_total` before overtime and extras existed."""
    hours = Decimal(trip.hours or 0)
    billed = hours if hours > 0 else Decimal(trip.min_hours or 0)
    subtotal = (Decimal(trip.rate or 0) * billed).quantize(CENTS)
    flat_discount = Decimal(trip.discount_flat or 0)
    if flat_discount > 0:
        discount = flat_discount.quantize(CENTS)
    else:
        discount = (subtotal * Decimal(trip.discount_pct or 0) / 100).quantize(CENTS)
    base = max(subtotal - discount, Decimal("0.00")).quantize(CENTS)
    flat_gratuity = Decimal(trip.gratuity_flat or 0)
    if flat_gratuity > 0:
        gratuity = flat_gratuity.quantize(CENTS)
    else:
        gratuity = (base * Decimal(trip.gratuity_pct or 0) / 100).quantize(CENTS)
    return (base + gratuity).quantize(CENTS)


def _discounted_rate(trip) -> Decimal:
    hours = Decimal(trip.hours or 0)
    billed = hours if hours > 0 else Decimal(trip.min_hours or 0)
    if billed <= 0:
        return Decimal("0.00")
    subtotal = (Decimal(trip.rate or 0) * billed).quantize(CENTS)
    flat_discount = Decimal(trip.discount_flat or 0)
    if flat_discount > 0:
        discount = flat_discount.quantize(CENTS)
    else:
        discount = (subtotal * Decimal(trip.discount_pct or 0) / 100).quantize(CENTS)
    base = max(subtotal - discount, Decimal("0.00")).quantize(CENTS)
    return (base / billed).quantize(CENTS)


def freeze_booked_totals(Reservation) -> int:
    """Step 1. Returns how many trips were frozen."""
    frozen = 0
    pending = []
    for trip in Reservation.objects.filter(lead__status=BOOKED, booked_total__isnull=True).iterator(
        chunk_size=500
    ):
        trip.booked_total = _line_total(trip)
        pending.append(trip)
        if len(pending) >= 500:
            Reservation.objects.bulk_update(pending, ["booked_total"])
            frozen += len(pending)
            pending = []
    if pending:
        Reservation.objects.bulk_update(pending, ["booked_total"])
        frozen += len(pending)
    return frozen


def move_final_overtime_onto_trips(Reservation, Charge, TripReview) -> int:
    """Step 2. Returns how many trips received overtime hours."""
    moved = 0
    finals = Charge.objects.filter(kind="final", status="succeeded").select_related("plan")
    for charge in finals.iterator(chunk_size=200):
        lead_id = charge.plan.lead_id
        reviews = TripReview.objects.filter(
            reservation__lead_id=lead_id,
            completed_at__isnull=False,
            overtime_waived=False,
            billable_overtime_minutes__gt=0,
        ).select_related("reservation")
        total = Decimal("0.00")
        split: list[tuple[object, Decimal]] = []
        for review in reviews:
            trip = review.reservation
            hours = (Decimal(review.billable_overtime_minutes) / 60).quantize(
                CENTS, rounding=ROUND_HALF_UP
            )
            split.append((trip, hours))
            total += (hours * _discounted_rate(trip)).quantize(CENTS)
        charged = Decimal(charge.overtime_amount or 0).quantize(CENTS)
        if total != charged:
            logger.warning(
                "Lead %s: FINAL charge %s billed overtime %s but the trips would carry %s;"
                " left at 0 hours",
                lead_id,
                charge.pk,
                charged,
                total,
            )
            continue
        for trip, hours in split:
            trip.overtime_hours = hours
            trip.save(update_fields=["overtime_hours"])
            moved += 1
    return moved


def forwards(apps, schema_editor):
    Reservation = apps.get_model("reservations", "Reservation")
    Charge = apps.get_model("payments", "Charge")
    TripReview = apps.get_model("reservations", "TripReview")
    freeze_booked_totals(Reservation)
    move_final_overtime_onto_trips(Reservation, Charge, TripReview)


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0023_pricing_on_the_trip"),
        ("payments", "0013_payable_qbo_review_fixes"),
        ("leads", "0018_lead_fees_acknowledged"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
