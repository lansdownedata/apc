"""The message rows (spec C section 2) as the rest of the app reads them: all of them in
one query, one by key, and a row's timing as the (amount, unit) a person would say it."""

from datetime import timedelta

from . import triggers
from .defaults import TIMING_UNITS
from .models import MessageTemplate

UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}


def load() -> dict[str, MessageTemplate]:
    """Every message row, keyed by its key, in one query."""
    return {row.key: row for row in MessageTemplate.objects.all()}


def get(key: str) -> MessageTemplate | None:
    return MessageTemplate.objects.filter(key=key).first()


def timing_of(key: str, rows: dict[str, MessageTemplate] | None = None) -> tuple[int, str] | None:
    """The row's timing in its largest whole unit, e.g. 720 minutes is (12, "hours"). None
    for a row that doesn't exist or whose trigger has no timing (staff clicks, schedules).
    ``rows`` is a `load()` already read."""
    row = get(key) if rows is None else rows.get(key)
    if row is None:
        return None
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is None or trigger.kind in ("staff_click", "schedule"):
        return None
    return in_largest_unit(row.offset)


def in_largest_unit(offset: timedelta) -> tuple[int, str]:
    """A span as the (amount, unit) a person would say it: 720 minutes is (12, "hours")."""
    seconds = abs(int(offset.total_seconds()))
    for unit in reversed(TIMING_UNITS):
        if seconds and seconds % UNIT_SECONDS[unit] == 0:
            return seconds // UNIT_SECONDS[unit], unit
    return seconds // 60, "minutes"
