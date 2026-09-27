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
4. **Post-trip** (APC-58) — trips that finished (Done, or past their scheduled end plus
   the grace with no status) enter the post-trip workflow, and any stage whose
   predecessors closed through a path with no hook opens its successors.
5. **Escalate** — overdue tasks (APC-52).

The cost is a fixed handful of queries per batch of orders, not per task — plus one
`ensure_tasks` per order that actually has a stage to open this tick.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.leads.models import Lead
from apps.reservations.models import Reservation

from . import escalation, post_trip
from .definitions import POST_TRIP_KINDS, PREDECESSOR_KINDS, REGISTRY, Level, _always
from .facts import load_facts
from .models import Task, TaskConfig
from .services import _evaluate_many, _predecessors_closed, sync

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


def _evaluate(now) -> list[Task]:
    cutoff = (now - REOPEN_LOOKBACK).date()
    upcoming = Lead.objects.filter(
        status=Lead.Status.BOOKED, reservations__pickup_date__gte=cutoff
    ).values("pk")
    candidates = Task.objects.filter(lead__status=Lead.Status.BOOKED).filter(
        Q(status__in=Task.UNRESOLVED)
        | Q(status=Task.Status.DONE, completed_by__isnull=True, lead_id__in=upcoming)
    )
    lead_ids = sorted(set(candidates.values_list("lead_id", flat=True)))
    changed: list[Task] = []
    for start in range(0, len(lead_ids), BATCH):
        chunk = lead_ids[start : start + BATCH]
        facts = load_facts(Lead.objects.filter(pk__in=chunk))
        changed += _evaluate_many(candidates.filter(lead_id__in=chunk), facts)
    return changed


def _entering(config: TaskConfig, now) -> set[int]:
    """Orders with a trip that has finished and has no `ops_review` yet. One query over
    the trips near today; the end-time rule runs in Python (`post_trip.entered`)."""
    grace = timedelta(hours=config.post_trip_grace_hours)
    today = now.date()
    # ±1 day around the window: `pickup_date` is trip-local, `now` is UTC.
    window = (today - post_trip.LOOKBACK - timedelta(days=1), today + timedelta(days=1))
    trips = (
        Reservation.objects.filter(lead__status=Lead.Status.BOOKED, pickup_date__range=window)
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .exclude(Exists(Task.objects.filter(reservation=OuterRef("pk"), kind="ops_review")))
    )
    return {t.lead_id for t in trips if post_trip.entered(t, now=now, grace=grace)}


def _stalled(now) -> set[int]:
    """Orders where a post-trip stage closed but the next one never opened — closed in the
    admin, or a hook that failed. Only kinds that always follow count as missing: a payable
    that doesn't exist on an in-house trip isn't a gap."""
    recent = Task.objects.filter(
        kind__in=PREDECESSOR_KINDS,
        status__in=Task.CLOSED,
        completed_at__gte=now - post_trip.LOOKBACK,
        lead__status=Lead.Status.BOOKED,
    ).values("lead_id")
    rows = Task.objects.filter(kind__in=POST_TRIP_KINDS, lead_id__in=recent).values_list(
        "lead_id", "reservation_id", "kind", "status"
    )
    statuses: dict[int, dict] = {}
    for lead_id, trip_id, kind, status in rows:
        statuses.setdefault(lead_id, {})[(trip_id, kind)] = status
    # Trip-level only: an order-level follower waits on every trip, and the hook on the
    # last one to close is what opens it.
    followers = [
        k for k in REGISTRY if k.opens_after and k.applies is _always and k.level == Level.TRIP
    ]
    stalled = set()
    for lead_id, by_key in statuses.items():
        trips = {trip_id for trip_id, _ in by_key}
        for kind in followers:
            if any(
                (trip_id, kind.key) not in by_key and _predecessors_closed(kind, trip_id, by_key)
                for trip_id in trips
            ):
                stalled.add(lead_id)
    return stalled


def _post_trip(config: TaskConfig, now, evaluated: list[Task]) -> int:
    advanced = {t.lead_id for t in evaluated if t.kind in PREDECESSOR_KINDS and t.is_closed}
    lead_ids = _entering(config, now) | _stalled(now) | advanced
    return sum(len(sync(Lead(pk=lead_id))) for lead_id in sorted(lead_ids))


def run_tasks() -> int:
    config = TaskConfig.load()
    if not config.enabled:
        return 0
    now = timezone.now()
    processed = _sweep(now)
    processed += _open(now)
    evaluated = _evaluate(now)
    processed += len(evaluated)
    processed += _post_trip(config, now, evaluated)
    processed += escalation.run(config, now)
    return processed
