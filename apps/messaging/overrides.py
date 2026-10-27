"""Resolve a message: the owner's `MessageTemplate` row layered over the code default."""

from dataclasses import replace
from datetime import timedelta

from .models import MessageTemplate
from .touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING, TouchPointTemplate

TIMING_UNITS = ("minutes", "hours", "days")
_UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}


def default(kind: str) -> TouchPointTemplate:
    return THANK_YOU_WEDDING if kind == "thank_you_wedding" else TEMPLATES[kind]


def load() -> dict[str, MessageTemplate]:
    """Every override row, keyed by kind, in one query."""
    return {row.kind: row for row in MessageTemplate.objects.all()}


def _row_offset(row, base: TouchPointTemplate) -> timedelta | None:
    if not row or not row.timing_amount or row.timing_unit not in TIMING_UNITS:
        return None
    span = timedelta(**{row.timing_unit: row.timing_amount})
    return -span if base.offset < timedelta(0) else span


def effective(kind: str, rows: dict | None = None) -> TouchPointTemplate:
    base = default(kind)
    rows = load() if rows is None else rows
    row = rows.get(kind)
    if row is None:
        return base
    changes = {f: getattr(row, f) for f in ("subject", "email_body", "sms_body") if getattr(row, f)}
    offset = _row_offset(row, base)
    if offset is not None:
        changes["offset"] = offset
    return replace(base, **changes)


def timing_of(kind: str, rows: dict | None = None) -> tuple[int, str] | None:
    """The effective (amount, unit) for a kind that has a timing, else None."""
    if default(kind).anchor == "triggered":
        return None
    seconds = abs(int(effective(kind, rows).offset.total_seconds()))
    for unit in reversed(TIMING_UNITS):
        if seconds and seconds % _UNIT_SECONDS[unit] == 0:
            return seconds // _UNIT_SECONDS[unit], unit
    return seconds // 60, "minutes"
