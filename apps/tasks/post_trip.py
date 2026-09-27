"""When a trip enters the post-trip workflow (APC-58).

A trip has *ended* when LimoAnywhere (or dispatch) marks it Done, **or** when its
scheduled end has passed by `TaskConfig.post_trip_grace_hours` with no status at all — we
still run in parallel with LA, so a Done that never arrives must not strand it.

Review is per **order** (Trip Review design, APC-59): an order enters once every live
trip has ended — a wedding weekend is reviewed as a whole. Cancelled trips (every
"Cancelled" phase, No Show included) count as finished and need no review. Entering
creates a Stage 1 `ops_review` task on each live trip; the later stages chain off it
through `TaskKind.opens_after` (see `definitions.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from apps.reservations.models import Reservation

# How far back a finished trip still enters. Without a floor, the first deploy (or an edit
# to an old order) would open an overdue review for every trip ever run — and LA has
# already written Done back on most of them.
LOOKBACK = timedelta(days=7)


def scheduled_end(trip: Reservation) -> datetime | None:
    """The drop-off when one is set, else pickup + billed hours. In the trip's zone."""
    start = trip.pickup_at
    if start is None:
        return None
    if trip.dropoff_time is not None:
        zone = ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)
        day = trip.dropoff_date or trip.pickup_date
        end = datetime.combine(day, trip.dropoff_time, tzinfo=zone)
        # A drop-off clock earlier than pickup with no date of its own is past midnight.
        if trip.dropoff_date is None and end < start:
            end += timedelta(days=1)
        return end
    return start + timedelta(hours=float(trip.billed_hours))


def has_ended(trip: Reservation, *, now: datetime, grace: timedelta) -> bool:
    """Done, or its scheduled end is more than `grace` behind `now`. Never a cancelled trip."""
    if trip.is_cancelled:
        return False
    if trip.trip_status == Reservation.TripStatus.DONE:
        return True
    end = scheduled_end(trip)
    return end is not None and end + grace <= now


def order_finished_at(trips, *, now: datetime, grace: timedelta) -> datetime | None:
    """When an order finished: every live trip has ended (cancelled trips count as done
    and need nothing), so this is the latest of their ends. None while any live trip is
    still to run, or when there's no live trip at all.

    A trip marked Done before its scheduled end finished then, not at the schedule.
    """
    live = [t for t in trips if not t.is_cancelled]
    if not live or not all(has_ended(t, now=now, grace=grace) for t in live):
        return None
    ends = [min(scheduled_end(t) or now, now) for t in live]
    return max(ends)


def order_entered(trips, *, now: datetime, grace: timedelta) -> bool:
    """The order has finished, recently enough that its review is still owed (LOOKBACK
    runs from its last trip)."""
    finished = order_finished_at(trips, now=now, grace=grace)
    return finished is not None and finished >= now - LOOKBACK
