"""Read-side queries for the board. Kept out of views so the query shape is testable."""

from __future__ import annotations

from datetime import date

from django.db.models import Prefetch

from apps.leads.models import Lead
from apps.reservations.models import TRIP_PHASE_BY_STATUS, Reservation, Stop

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
        .order_by("pickup_time", "pk")
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
