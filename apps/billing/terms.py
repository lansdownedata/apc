"""One resolver for payment terms (APC-96): label, QuickBooks id and due date.

A stored term is either a legacy `Terms` code or an exact QuickBooks term name. Every
screen, the sync and the due-date math read it through `resolve`, so they cannot disagree.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from apps.integrations import qbo

from .models import QboTerm, Terms

# Legacy codes: days to due when QuickBooks has no matching term carrying its own rule.
TERM_DAYS: dict[str, int] = {
    Terms.DUE_ON_RECEIPT: 0,
    Terms.NET_15: 15,
    Terms.NET_30: 30,
    Terms.NET_45: 45,
}
UNKNOWN_DAYS = 30


def _realm() -> str:
    # Looked up through qbo_sync at call time (a lazy import: qbo_sync imports this module).
    from . import qbo_sync

    return qbo_sync.current_realm()


def _usable():
    from . import qbo_sync

    return qbo_sync._usable_terms()


def _add_months(year: int, month: int, extra: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + extra
    return index // 12, index % 12 + 1


@dataclass(frozen=True)
class TermRule:
    kind: str  # "STANDARD" or "DATE_DRIVEN"
    days: int = 0
    day_of_month: int = 0
    cutoff: int = 0

    def due(self, invoice_date: date) -> date:
        """STANDARD: invoice date plus `days`. DATE_DRIVEN: the `day_of_month` of the invoice
        month, or of the next month when the invoice day is past `cutoff` (if set); the day is
        clamped to the month's length. Semantics unverified on the sandbox."""
        if self.kind != "DATE_DRIVEN":
            return invoice_date + timedelta(days=self.days)
        extra = 1 if self.cutoff > 0 and invoice_date.day > self.cutoff else 0
        year, month = _add_months(invoice_date.year, invoice_date.month, extra)
        return date(year, month, min(self.day_of_month, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class ResolvedTerm:
    value: str
    label: str
    qbo_id: str | None
    rule: TermRule | None


def _rule_of(term: QboTerm) -> TermRule | None:
    if term.term_type == "DATE_DRIVEN":
        if term.day_of_month_due:
            return TermRule(
                "DATE_DRIVEN",
                day_of_month=term.day_of_month_due,
                cutoff=term.due_next_month_days or 0,
            )
        return TermRule("STANDARD", days=UNKNOWN_DAYS)
    if term.due_days is not None:
        return TermRule("STANDARD", days=term.due_days)
    return None


def is_quickbooks_name(value: str) -> bool:
    """True for a stored value that is not one of the legacy codes."""
    return value not in Terms.values


def resolve(value: str) -> ResolvedTerm:
    if not value:
        return ResolvedTerm("", "", None, None)
    if not is_quickbooks_name(value):
        label = Terms(value).label
        match = _usable().filter(name__iexact=label).first()
        rule = (match and _rule_of(match)) or TermRule("STANDARD", days=TERM_DAYS[value])
        return ResolvedTerm(value, label, match.qbo_id if match else None, rule)
    match = _usable().filter(name__iexact=value).first()
    if match is None:
        return ResolvedTerm(value, value, None, TermRule("STANDARD", days=UNKNOWN_DAYS))
    rule = _rule_of(match) or TermRule("STANDARD", days=UNKNOWN_DAYS)
    return ResolvedTerm(value, match.name, match.qbo_id, rule)


def labels(values: Iterable[str]) -> dict[str, str]:
    """`resolve(v).label` for many values in one read of the company's term names, so a
    screen listing many records costs the same as one. Nothing is read for no values."""
    wanted = set(values)
    if not wanted:
        return {}
    names = {}
    if any(is_quickbooks_name(v) for v in wanted if v):
        # The first in the model's own order wins, as `resolve`'s `.first()` does.
        for name in _usable().values_list("name", flat=True):
            names.setdefault(name.lower(), name)
    return {
        v: (Terms(v).label if not is_quickbooks_name(v) else names.get(v.lower(), v)) if v else ""
        for v in wanted
    }


def offer(options: list[tuple[str, str]], current: str, label: str) -> list[tuple[str, str]]:
    """`options` plus the record's stored value, when it isn't offered already: it replaces
    the entry with the same label (a legacy code keeps its code) and goes last."""
    if not current or current in {value for value, _ in options}:
        return options
    rest = [(v, lbl) for v, lbl in options if lbl.lower() != label.lower()]
    return [*rest, (current, label)]


def due(value: str, invoice_date: date) -> date:
    rule = resolve(value).rule or TermRule("STANDARD", days=UNKNOWN_DAYS)
    return rule.due(invoice_date)


def choices(current: str = "") -> list[tuple[str, str]]:
    """The picker's options: the company's own QuickBooks terms when connected and cached,
    else the legacy list. The record's current value is always present, as stored: a legacy
    code keeps its code and replaces the same-named QuickBooks entry."""
    names = (
        list(_usable().order_by("name").values_list("name", flat=True))
        if (qbo.is_connected())
        else []
    )
    options = [(n, n) for n in names] or list(Terms.choices)
    if current and current not in {value for value, _ in options}:
        options = offer(options, current, resolve(current).label)
    return options
