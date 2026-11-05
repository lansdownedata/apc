"""Quiet hours (spec C section 5, story 5, APC-114): a text that would land overnight waits.

The window is `NotificationConfig.quiet_start` to `quiet_end` (it may cross midnight; start
== end means off), judged in the trip's own zone, or the company's for a send with no trip.
`hold_until` answers when a text due now may go instead; the sender (`sender._send`) sends
the email part now and queues each text until then.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings

from . import triggers
from .models import MessageTemplate, NotificationConfig, TouchPoint

# Trigger kinds only a click, an invite or a report sends (`sender.send_now`): never held.
_NEVER_HELD = frozenset({"staff_click", "schedule"})
# Live trip statuses describe the car right now: they always go.
_LIVE = frozenset({"status_dispatched", "status_on_the_way", "status_arrived"})


def _zone(tp: TouchPoint) -> ZoneInfo:
    res = tp.reservation
    return ZoneInfo((res.pickup_timezone if res is not None else "") or settings.TIME_ZONE)


def window_end(now: datetime, start: time, end: time, zone: ZoneInfo) -> datetime | None:
    """The end of the window `now` falls in, in ``zone``; None outside it (or when off)."""
    if start == end:
        return None
    local = now.astimezone(zone)
    clock, day = local.time(), local.date()
    if start < end:
        inside = start <= clock < end
    else:  # crosses midnight: the evening's window ends tomorrow morning
        inside = clock >= start or clock < end
        if clock >= start:
            day += timedelta(days=1)
    if not inside:
        return None
    return datetime.combine(day, end, tzinfo=zone)


def hold_until(
    tp: TouchPoint,
    row: MessageTemplate,
    *,
    now: datetime,
    config: NotificationConfig | None = None,
) -> datetime | None:
    """When a text of ``row`` for ``tp``, due at ``now``, may go; None to send it now.

    None when quiet hours are off, for staff mail, for anything a staff click, an invite or
    a report sends, for a live trip status (Dispatched, On the way, Arrived), outside the
    window, and for a trip whose pickup is still ahead but comes before the window ends
    (held to then, it would arrive as the car does). A pickup already past is no reason to
    text at night. ``config`` is the settings row already read, so a batch reads it once."""
    if row.audience == MessageTemplate.Audience.STAFF or row.trigger in _LIVE:
        return None
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is not None and (trigger.kind in _NEVER_HELD or trigger.caller_sends):
        return None
    config = config or NotificationConfig.load()
    until = window_end(now, config.quiet_start, config.quiet_end, _zone(tp))
    if until is None:
        return None
    pickup = tp.reservation.pickup_at if tp.reservation is not None else None
    if pickup is not None and now <= pickup <= until:
        return None
    return until


def reason(until: datetime) -> str:
    """A held text's Delivery reason: "Quiet hours until 8:00 AM PST"."""
    return f"Quiet hours until {until:%-I:%M %p} {until.tzname()}"
