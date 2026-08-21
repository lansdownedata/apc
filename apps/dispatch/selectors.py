"""Read-side queries for the board. Kept out of views so the query shape is testable."""

from __future__ import annotations

from datetime import date

from django.db.models import Count, Exists, F, OuterRef, Prefetch

from apps.leads.models import Lead, VehicleType
from apps.reservations.models import TRIP_PHASE_BY_STATUS, Reservation, Stop
from apps.vendors.models import Vendor, VendorInsurance

from .models import Assignment

COVERAGE_UNCOVERED = "uncovered"
COVERAGE_OFFERED = "offered"
COVERAGE_CONFIRMED = "confirmed"

# A cancelled trip needs no coverage, so it would otherwise sit in the "uncovered" count
# forever. `Reservation.is_cancelled` is a property over TRIP_PHASE_BY_STATUS and can't be
# used in a filter — list the statuses whose phase is "Cancelled" instead.
CANCELLED_STATUSES = tuple(
    status
    for status in Reservation.TripStatus.values
    if TRIP_PHASE_BY_STATUS.get(status) == "Cancelled"
)


def board_trips(day: date) -> list[Reservation]:
    """Booked trips picking up on `day`, each decorated with its coverage and route ends.

    Coverage is derived from the active assignment rather than stored on the trip, so a
    declined offer puts the trip straight back in the uncovered bucket with no cleanup.

    Everything is resolved from prefetches in one pass because the template renders a
    whole day at once: `Reservation.pickup`/`dropoff` are properties over
    `stops.order_by("sequence")`, and that `.order_by()` builds a fresh queryset that
    ignores the prefetch cache — two extra queries per row if a caller touches them.
    `pickup_stop`/`dropoff_stop` exist so the template never has to.

    NULL pickup times are pinned to the top explicitly: MySQL (dev/test) sorts NULLs first
    and Postgres (prod) sorts them last, so without saying which we want the same day reads
    differently in the two environments. A booked trip with no pickup time is an exception
    the dispatcher has to resolve, so it belongs at the top rather than buried at the end.
    """
    trips = list(
        Reservation.objects.filter(lead__status=Lead.Status.BOOKED, pickup_date=day)
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .select_related("lead", "lead__contact", "vehicle")
        .prefetch_related(
            Prefetch("stops", queryset=Stop.objects.order_by("sequence")),
            Prefetch(
                "assignments",
                queryset=Assignment.objects.active().select_related("vendor"),
                to_attr="active_list",
            ),
        )
        .order_by(F("pickup_time").asc(nulls_first=True), "pk")
    )
    for trip in trips:
        stops = list(trip.stops.all())  # prefetched, already in sequence order
        trip.pickup_stop = stops[0] if stops else None
        trip.dropoff_stop = stops[-1] if len(stops) > 1 else None
        trip.active = trip.active_list[0] if trip.active_list else None
        trip.coverage = trip.active.status if trip.active else COVERAGE_UNCOVERED
    return trips


def strip_counts(trips: list[Reservation]) -> dict[str, int]:
    """Whole-day totals for the attention strip — never narrowed by the active filter."""
    counts = {COVERAGE_UNCOVERED: 0, COVERAGE_OFFERED: 0, COVERAGE_CONFIRMED: 0}
    for trip in trips:
        counts[trip.coverage] += 1
    return counts


def vendor_options(trip: Reservation, *, search: str = "", limit: int = 8) -> list[dict]:
    """Affiliates to offer this trip to — most-used first, or whatever the search matches.

    "Most used" counts every past assignment regardless of outcome: a vendor he offers to
    often is the one he reaches for, even when they sometimes decline. Search bypasses the
    ranking so the whole directory stays reachable from the drawer.

    Two queries whatever the vendor count, and it has to stay that way (see
    test_vendor_options_query_count_is_flat_regardless_of_vendor_count). That is why
    vehicle fit is an `Exists` subquery rather than a second prefetch: the one prefetch
    slot buys the insurance policies, which `insurance_summary()` needs in memory.
    """
    qs = (
        Vendor.objects.filter(status=Vendor.Status.ACTIVE)
        .annotate(
            used=Count("assignments"),
            fits_vehicle=Exists(
                VehicleType.objects.filter(pk=trip.vehicle_id, vendors=OuterRef("pk"))
            ),
        )
        .prefetch_related(
            Prefetch(
                "policies",
                queryset=VendorInsurance.objects.only("id", "vendor_id", "expiry_date"),
            )
        )
    )
    term = (search or "").strip()
    if term:
        qs = qs.filter(name__icontains=term)
    qs = qs.order_by("-used", "name")[:limit]

    return [
        {
            "vendor": vendor,
            "used": vendor.used,
            "fits_vehicle": vendor.fits_vehicle,
            "insurance": vendor.insurance_summary(),
        }
        for vendor in qs
    ]
