"""When a trip enters the post-trip workflow (APC-58).

A trip enters when LimoAnywhere (or dispatch) marks it Done, **or** when its scheduled
end has passed by `TaskConfig.post_trip_grace_hours` with no status at all — we still run
in parallel with LA, so a Done that never arrives must not strand the trip. Cancelled
trips (every "Cancelled" phase, No Show included) never enter.

Entering creates the Stage 1 `ops_review` task; the later stages chain off it through
`TaskKind.opens_after` (see `definitions.py`).
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


def entered(trip: Reservation, *, now: datetime, grace: timedelta) -> bool:
    """`has_ended`, and recently enough that its review is still owed (see LOOKBACK)."""
    if not has_ended(trip, now=now, grace=grace):
        return False
    end = scheduled_end(trip)
    return end is None or end >= now - LOOKBACK
