"""Wall-clock display for a reservation's pickup, with a zone label when needed."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


@register.filter
def zone_clock(dt, tz_name) -> str:
    """An aware datetime as `Sun Nov 8 · 10:27 AM PST` in the IANA zone `tz_name`.

    The abbreviation is shown only when that zone differs from TIME_ZONE; a blank zone
    means TIME_ZONE, and so does an unknown one, so one bad row never breaks a list. For
    rows that carry their own zone rather than a reservation's.
    """
    if not dt:
        return ""
    zone = tz_name or settings.TIME_ZONE
    try:
        local = dt.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        zone = settings.TIME_ZONE
        local = dt.astimezone(ZoneInfo(zone))
    label = dateformat.format(local, "D M j · g:i A")
    if zone == settings.TIME_ZONE:
        return label
    return f"{label} {local.tzname()}"
