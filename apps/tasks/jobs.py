"""The `run-tasks` cron (APC-51) — keeps tasks correct without anyone touching them.

Every 15 minutes via cron-job.org → `POST /cron/run-tasks/`. Idempotent: a second tick
with nothing new changes nothing and returns 0. Each tick:

1. **Sweep** — unresolved tasks on an order that's no longer booked, or on a cancelled
   trip, become not-applicable. The cancel hooks (`release_trips`) catch most of these;
   this catches a status written by a path with no hook.
2. **Open** — scheduled tasks whose `opens_at` has passed.
3. **Re-evaluate** — auto-complete predicates over every unresolved task, plus the
   system-closed tasks on upcoming trips (so a withdrawn offer reopens its task). This is
   what closes work whose data arrived with no seam hook: the GNet callback, the admin,
   the LA webhook, a touch-point the send loop just delivered.
4. **Escalate** — overdue tasks (APC-52).

The cost is a fixed handful of queries per batch of orders, not per task.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.leads.models import Lead

from . import escalation
from .facts import load_facts
from .models import Task, TaskConfig
from .services import _evaluate_many

BATCH = 200
# System-closed tasks are re-checked only while their order still has a trip ahead (or
# just behind) — a finished order's history shouldn't be re-litigated every 15 minutes.
REOPEN_LOOKBACK = timedelta(days=1)


def _sweep(now) -> int:
    na = {
        "status": Task.Status.NOT_APPLICABLE,
        "completed_at": now,
        "completed_by": None,
        "updated_at": now,
    }
    unresolved = Task.objects.filter(status__in=Task.UNRESOLVED)
    count = unresolved.exclude(lead__status=Lead.Status.BOOKED).update(**na)
    count += unresolved.filter(reservation__trip_status__in=CANCELLED_STATUSES).update(**na)
    return count


def _open(now) -> int:
    return Task.objects.filter(status=Task.Status.SCHEDULED, opens_at__lte=now).update(
        status=Task.Status.OPEN, updated_at=now
    )


def _evaluate(now) -> int:
    cutoff = (now - REOPEN_LOOKBACK).date()
    upcoming = Lead.objects.filter(
        status=Lead.Status.BOOKED, reservations__pickup_date__gte=cutoff
    ).values("pk")
    candidates = Task.objects.filter(lead__status=Lead.Status.BOOKED).filter(
        Q(status__in=Task.UNRESOLVED)
        | Q(status=Task.Status.DONE, completed_by__isnull=True, lead_id__in=upcoming)
    )
    lead_ids = sorted(set(candidates.values_list("lead_id", flat=True)))
    changed = 0
    for start in range(0, len(lead_ids), BATCH):
        chunk = lead_ids[start : start + BATCH]
        facts = load_facts(Lead.objects.filter(pk__in=chunk))
        changed += _evaluate_many(candidates.filter(lead_id__in=chunk), facts)
    return changed


def run_tasks() -> int:
    config = TaskConfig.load()
    if not config.enabled:
        return 0
    now = timezone.now()
    processed = _sweep(now)
    processed += _open(now)
    processed += _evaluate(now)
    processed += escalation.run(config, now)
    return processed
