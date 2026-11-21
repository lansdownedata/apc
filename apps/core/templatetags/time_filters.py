"""Wall-clock display for a reservation's pickup, with a zone label when needed."""

from zoneinfo import ZoneInfo

from django import template
from django.conf import settings
from django.utils import dateformat

register = template.Library()


@register.filter
def pickup_clock(reservation, when=None) -> str:
    """`7:30 AM` or `7:30 AM PDT` when the trip's zone differs from TIME_ZONE.

    Optional `when` is a `datetime.time` (a stop's `scheduled_time`); default is
    `reservation.pickup_time`.
    """
    clock = reservation.pickup_time if when is None else when
    if not clock:
        return ""
    label = dateformat.format(clock, "g:i A")
    abbrev = reservation.pickup_tz_abbrev
    return f"{label} {abbrev}" if abbrev else label


@register.filter
def trip_clock(dt, reservation) -> str:
    """An aware UTC datetime (e.g. a confirmation timestamp) rendered in the trip's own
    pickup timezone — `Sep 4, 7:30 AM` or `Sep 4, 7:30 AM PDT` when that zone differs from
    TIME_ZONE. Never show `dt` with a bare `date` filter; that renders in TIME_ZONE, not
    the trip's zone, per the timezone-handling rule.
    """
    if not dt or reservation is None:
        return ""
    zone = reservation.pickup_timezone or settings.TIME_ZONE
    local = dt.astimezone(ZoneInfo(zone))
    label = dateformat.format(local, "M j, g:i A")
    abbrev = reservation.pickup_tz_abbrev
    return f"{label} {abbrev}" if abbrev else label
