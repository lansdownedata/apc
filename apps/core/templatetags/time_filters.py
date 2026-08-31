"""Wall-clock display for a reservation's pickup, with a zone label when needed."""

from django import template
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
