"""Settings overrides of checklist due timing, read as one lookup, saved and re-dated."""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Department
from apps.leads.models import Lead

from . import services
from .definitions import (
    FARM_IN,
    KINDS,
    AfterLastPickup,
    AfterOpen,
    AfterOpenBusinessDays,
    AfterOrderEnd,
    BeforeExpiry,
    BeforePickup,
    apply_schedule,
    default_timing,
)
from .models import Task, TaskSchedule

logger = logging.getLogger(__name__)

Key = tuple[str, str]
Timing = tuple[int, str]

STANDARD, FARM = TaskSchedule.Variant.STANDARD, TaskSchedule.Variant.FARM_IN
HOURS, DAYS, BUSINESS_DAYS = (
    TaskSchedule.Unit.HOURS,
    TaskSchedule.Unit.DAYS,
    TaskSchedule.Unit.BUSINESS_DAYS,
)
# The most a Settings row may hold, per unit (a year of days, a year of hours, six weeks).
MAX_AMOUNT = {DAYS: 365, HOURS: 8760, BUSINESS_DAYS: 30}


@dataclass(frozen=True)
class ScheduleRow:
    """One line of the Settings -> Checklist schedule page."""

    key: str
    variant: str
    group: str
    anchor: str
    units: tuple[str, ...]
    note: str = ""

    @property
    def label(self) -> str:
        return KINDS[self.key].label

    @property
    def department(self) -> str:
        return Department(KINDS[self.key].department).label


def _kind(key: str, variant: str):
    """The kind as its variant ships in code: the farm-in clock for FARM_IN rows."""
    kind = KINDS[key]
    if variant == FARM and key in FARM_IN:
        kind = replace(kind, **FARM_IN[key])
    return kind


def _anchor(key: str, variant: str) -> str:
    due = _kind(key, variant).due
    if isinstance(due, BeforePickup):
        return "before pickup"
    if isinstance(due, AfterOrderEnd):
        return "after the trip ends"
    if isinstance(due, AfterLastPickup):
        return "after the last trip"
    if isinstance(due, BeforeExpiry):
        return "before the policy expires"
    if key in ("deposit_received", "contract_signed"):
        return "after booking"
    return "after it opens"


def _units(key: str, variant: str) -> tuple[str, ...]:
    due = _kind(key, variant).due
    if isinstance(due, AfterOpenBusinessDays):
        return (BUSINESS_DAYS,)
    if isinstance(due, AfterLastPickup | BeforeExpiry):
        return (DAYS,)
    return (HOURS, DAYS)


_BALANCE_NOTE = "Opens when the balance is charged (30 days before pickup)."
_RENEWAL_NOTE = "Opens 30 days before expiry."


def _build_rows() -> tuple[ScheduleRow, ...]:
    groups = (
        ("Booking", ("deposit_received", "contract_signed")),
        (
            "Before the trip",
            (
                "wedding_names",
                "day_of_contact",
                "final_itinerary",
                "affiliate_assigned",
                "affiliate_confirmed",
                "driver_assigned",
                "driver_info_received",
                "driver_released",
                "details_finalized",
                "final_balance_paid",
            ),
        ),
        (
            "After the trip",
            (
                "ops_review",
                "overtime_invoiced",
                "affiliate_payable_approved",
                "affiliate_paid",
                "thank_you_sent",
                "future_booking_followup",
            ),
        ),
        (
            "Requests & affiliates",
            ("complaint_followup", "planner_change_request", "insurance_renewal"),
        ),
    )
    notes = {"final_balance_paid": _BALANCE_NOTE, "insurance_renewal": _RENEWAL_NOTE}
    rows = []
    for group, keys in groups:
        for key in keys:
            variants = (STANDARD, FARM) if key in FARM_IN else (STANDARD,)
            for variant in variants:
                note = notes.get(key, "") if variant == STANDARD else ""
                anchor, units = _anchor(key, variant), _units(key, variant)
                rows.append(ScheduleRow(key, variant, group, anchor, units, note))
    return tuple(rows)


ROWS: tuple[ScheduleRow, ...] = _build_rows()
_ROW_INDEX = {(r.key, r.variant): r for r in ROWS}


def load() -> dict[Key, Timing]:
    """Every override as (kind, variant) -> (amount, unit). One query."""
    rows = TaskSchedule.objects.values_list("kind", "variant", "amount", "unit")
    return {(kind, variant): (amount, unit) for kind, variant, amount, unit in rows}


def code_default(key: str, variant: str) -> Timing:
    """The timing the code ships for `key`: the farm-in clock for FARM_IN, else standard."""
    kind = KINDS[key]
    if variant == TaskSchedule.Variant.FARM_IN and key in FARM_IN:
        kind = replace(kind, **FARM_IN[key])
    return default_timing(kind)


def save(values: dict[Key, Timing], *, user) -> int:
    """Store the posted timings and re-date open work for the ones that changed.

    A value equal to its code default removes the row, so "back to default" leaves no
    trace. Returns how many tasks were re-dated."""
    current = load()
    changed: set[Key] = set()
    with transaction.atomic():
        for key, timing in values.items():
            kind, variant = key
            timing = (int(timing[0]), timing[1])
            default = code_default(kind, variant)
            if timing == default:
                TaskSchedule.objects.filter(kind=kind, variant=variant).delete()
            else:
                TaskSchedule.objects.update_or_create(
                    kind=kind,
                    variant=variant,
                    defaults={"amount": timing[0], "unit": timing[1], "updated_by": user},
                )
            if timing != current.get(key, default):
                changed.add(key)
    return redate(changed) if changed else 0


def redate(keys: set[Key]) -> int:
    """Re-date every unresolved task of the given kinds (both variants); returns the count."""
    from . import vendor_tasks

    kinds = {kind for kind, _ in keys}
    total = 0
    lead_kinds = kinds - {vendor_tasks.KIND}
    if lead_kinds:
        leads = (
            Task.objects.filter(kind__in=lead_kinds, status__in=Task.UNRESOLVED, lead__isnull=False)
            .values_list("lead_id", flat=True)
            .distinct()
        )
        for lead in Lead.objects.filter(pk__in=list(leads)):
            try:
                total += services.reschedule(lead)
            except Exception:
                logger.exception("schedule re-date failed for lead %s", lead.pk)
    if vendor_tasks.KIND in kinds:
        total += vendor_tasks.redate()
    return total


def _stamp(moment: datetime) -> str:
    """`Sat Nov 14, 7:30 PM EST` — the zone abbreviation is part of every shown time."""
    return moment.strftime("%a %b %-d, %-I:%M %p %Z")


def preview(key: str, variant: str, amount: int, unit: str, *, now: datetime | None = None) -> str:
    """A worked example of one row's timing, computed by the real due-date classes.

    The sample pickup is the next Saturday at 7:30 PM in the business timezone; open-anchored
    items sample "opened Monday 9:00 AM" of that week, and an expiry falls on that Saturday.
    Raises ValueError for an unknown row, a unit the row doesn't offer, or an amount outside
    its bounds."""
    row = _ROW_INDEX.get((key, variant))
    if row is None:
        raise ValueError(f"unknown checklist item {key!r} ({variant!r})")
    if unit not in row.units:
        raise ValueError(f"{unit!r} is not an option for {row.label}")
    if not 0 <= amount <= MAX_AMOUNT[unit]:
        raise ValueError(f"amount must be between 0 and {MAX_AMOUNT[unit]} {unit}")

    zone = timezone.get_current_timezone()
    today = (now or timezone.now()).astimezone(zone).date()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    pickup = datetime.combine(saturday, time(19, 30), tzinfo=zone)
    opened = datetime.combine(saturday - timedelta(days=5), time(9, 0), tzinfo=zone)

    due = apply_schedule(_kind(key, variant), amount, unit).due
    if isinstance(due, BeforePickup):
        start, at = f"{_stamp(pickup)} pickup", due.resolve(pickup)
    elif isinstance(due, AfterOrderEnd):
        start, at = f"Trip ends {_stamp(pickup)}", due.resolve(pickup)
    elif isinstance(due, AfterLastPickup):
        start, at = f"Last trip {_stamp(pickup)}", due.resolve(pickup)
    elif isinstance(due, BeforeExpiry):
        start, at = f"Policy expires {saturday.strftime('%a %b %-d')}", due.resolve(saturday)
    else:
        assert isinstance(due, AfterOpen)
        start, at = f"Opened {_stamp(opened)}", due.resolve_from(opened)
    return f"{start} → due {_stamp(at.astimezone(zone))}"
