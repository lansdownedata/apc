"""Settings overrides of checklist due timing, read as one lookup, saved and re-dated."""

import logging
from dataclasses import replace

from django.db import transaction

from apps.leads.models import Lead

from . import services
from .definitions import FARM_IN, KINDS, default_timing
from .models import Task, TaskSchedule

logger = logging.getLogger(__name__)

Key = tuple[str, str]
Timing = tuple[int, str]


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
