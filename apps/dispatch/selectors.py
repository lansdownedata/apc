"""Read-side queries for the board. Kept out of views so the query shape is testable."""

from __future__ import annotations

from datetime import date

from django.db.models import Count, Exists, F, OuterRef, Prefetch

from apps.fleet.models import RENEWAL_PREFETCH, Driver, Vehicle
from apps.leads.models import Lead, VehicleType
from apps.reservations.models import TRIP_PHASE_BY_STATUS, Reservation, Stop
from apps.vendors.models import Vendor, VendorInsurance

from .board_filters import BoardFilters
from .models import Assignment, DispatchException, GnetEvent

_TIER_RANK = {DispatchException.Tier.WARNING: 1, DispatchException.Tier.CRITICAL: 2}

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


def confirmed_assignment(reservation: Reservation) -> Assignment | None:
    """The trip's active coverage, or None. The one place this lookup lives — the
    reservation-lifecycle messaging (recipient, driver info, affiliate payout) needs it in
    several spots and used to re-type it each time.

    Reads through `.assignments.all()` rather than `.filter(status=...)`: a related
    manager's `.filter()` always issues a fresh query, but `.all()` returns a prefetched
    queryset's cache when a caller already did `prefetch_related("...assignments...")`.
    """
    for a in reservation.assignments.all():
        if a.status == Assignment.Status.CONFIRMED:
            return a
    return None


def board_trips(filters: BoardFilters) -> list[Reservation]:
    """Booked trips picking up inside `filters`' date window, narrowed by its
    vehicle-type / customer / linked-set filters, each decorated with coverage + route ends.

    Coverage is derived from the active assignment rather than stored on the trip, so a
    declined offer puts the trip straight back in the uncovered bucket with no cleanup.

    Everything is resolved from prefetches in one pass because the template renders the
    whole window at once: `Reservation.pickup`/`dropoff` are properties over
    `stops.order_by("sequence")`, and that `.order_by()` builds a fresh queryset that
    ignores the prefetch cache — two extra queries per row if a caller touches them.
    `pickup_stop`/`dropoff_stop` exist so the template never has to.

    `pickup_date` is a naive, trip-local date on the row (never re-derived), so a plain
    `__range` on it already respects the trip's own date boundary. NULL pickup times are
    pinned to the top of each day explicitly: MySQL (dev/test) sorts NULLs first and
    Postgres (prod) sorts them last, so without saying which the same day reads
    differently in the two environments. A booked trip with no pickup time is an exception
    the dispatcher has to resolve, so it belongs at the top.
    """
    qs = (
        Reservation.objects.filter(
            lead__status=Lead.Status.BOOKED,
            pickup_date__range=(filters.start, filters.end),
        )
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .select_related("lead", "lead__contact", "vehicle")
        .prefetch_related(
            Prefetch(
                "stops",
                queryset=Stop.objects.select_related(
                    "airline", "airport", "flight", "flight__airport", "flight__airline"
                ).order_by("sequence"),
            ),
            Prefetch(
                "assignments",
                queryset=Assignment.objects.active().select_related("vendor", "driver", "vehicle"),
                to_attr="active_list",
            ),
            Prefetch(
                "dispatch_exceptions",
                queryset=DispatchException.objects.filter(resolved_at__isnull=True),
                to_attr="open_exceptions",
            ),
        )
        .order_by("pickup_date", F("pickup_time").asc(nulls_first=True), "pk")
    )
    if filters.vehicle_type_id is not None:
        qs = qs.filter(vehicle_id=filters.vehicle_type_id)
    if filters.contact_id is not None:
        qs = qs.filter(lead__contact_id=filters.contact_id)
    if filters.group_key:
        qs = qs.filter(group_key=filters.group_key)

    trips = list(qs)
    for trip in trips:
        stops = list(trip.stops.all())  # prefetched, already in sequence order
        trip.pickup_stop = stops[0] if stops else None
        trip.dropoff_stop = stops[-1] if len(stops) > 1 else None
        trip.active = trip.active_list[0] if trip.active_list else None
        trip.coverage = trip.active.status if trip.active else COVERAGE_UNCOVERED
        trip.exception_tier = max(
            (e.tier for e in trip.open_exceptions), key=_TIER_RANK.get, default=""
        )
    return trips


def exception_tally(trips: list[Reservation]) -> dict[str, int]:
    """How many trips in the window carry an open exception, by their worst tier."""
    out = {DispatchException.Tier.WARNING: 0, DispatchException.Tier.CRITICAL: 0}
    for trip in trips:
        if trip.exception_tier:
            out[trip.exception_tier] += 1
    return out


def day_groups(trips: list[Reservation]) -> list[tuple[date, list[Reservation], dict[str, int]]]:
    """Split an already-ordered board result into `(date, trips, counts)` per day.

    Drives the sticky day sub-headers in a week / range view. Iterates the list it is
    handed — no query of its own — and relies on `board_trips` ordering by `pickup_date`.
    """
    out: list[tuple[date, list[Reservation]]] = []
    for trip in trips:
        if not out or out[-1][0] != trip.pickup_date:
            out.append((trip.pickup_date, []))
        out[-1][1].append(trip)
    return [(day, day_trips, strip_counts(day_trips)) for day, day_trips in out]


def strip_counts(trips: list[Reservation]) -> dict[str, int]:
    """Whole-day totals for the attention strip — never narrowed by the active filter."""
    counts = {COVERAGE_UNCOVERED: 0, COVERAGE_OFFERED: 0, COVERAGE_CONFIRMED: 0}
    for trip in trips:
        counts[trip.coverage] += 1
    return counts


def offer_was_previewed(assignment: Assignment | None) -> bool:
    """True when this assignment's farm-out was staged in preview and never sent.

    Read off the `GnetEvent` rather than inferred from a blank `gnet_transaction_id`,
    which is equally blank after a genuine send failure — a very different situation
    with a very different alert.
    """
    if assignment is None or assignment.channel != Assignment.Channel.GNET:
        return False
    return assignment.gnet_events.filter(
        action=GnetEvent.Action.SEND_TRIP, result=GnetEvent.Result.PREVIEW
    ).exists()


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
            # `gnet_grid_id` is a plain column on the Vendor row this queryset already
            # fetched, so `is_gnet_capable` (a property over it) costs nothing extra —
            # the 2-query bound above still holds.
            "is_gnet": vendor.is_gnet_capable,
        }
        for vendor in qs
    ]


def in_house_options(trip: Reservation) -> dict:
    """Our own drivers and units for the drawer's In-house block.

    Drivers rank most-used first (every past assignment counts, the same basis as the
    vendor ranking), ties by name. Units that match the trip's vehicle class come first
    and are flagged; when the trip has no class set, nothing is flagged and they list by
    name. Each row carries its renewal roll-up so the drawer can warn — never block.

    Four queries whatever the fleet size (drivers + their renewals, units + theirs), and
    one when there are no active drivers, since an empty base queryset skips its
    prefetch — the panel's own query budget relies on that.
    """
    drivers = list(
        Driver.objects.filter(status=Driver.Status.ACTIVE)
        .annotate(used=Count("assignments"))
        .prefetch_related(RENEWAL_PREFETCH)
        .order_by("-used", "name")
    )
    if not drivers:
        return {"drivers": [], "vehicles": []}
    vehicles = list(
        Vehicle.objects.filter(status=Vehicle.Status.ACTIVE)
        .select_related("vehicle_type")
        .prefetch_related(RENEWAL_PREFETCH)
        .order_by("name")
    )
    if trip.vehicle_id is not None:
        vehicles.sort(key=lambda v: (v.vehicle_type_id != trip.vehicle_id, v.name.lower()))
    return {
        "drivers": [{"driver": d, "used": d.used, "renewal": d.renewal_summary()} for d in drivers],
        "vehicles": [
            {
                "vehicle": v,
                "fits_vehicle": trip.vehicle_id is not None
                and v.vehicle_type_id == trip.vehicle_id,
                "renewal": v.renewal_summary(),
            }
            for v in vehicles
        ],
    }
