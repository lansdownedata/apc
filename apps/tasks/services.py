"""Every Task write goes through here (APC-50).

- `ensure_tasks(lead)` creates the kinds that apply and don't exist yet, then evaluates.
  Idempotent. The unique constraint can't guard order-level rows on MySQL (their
  reservation is NULL), so the check-then-create runs under `select_for_update()` on the
  lead — the same pattern as `dispatch.services._claim`.
- `evaluate_lead` / `evaluate` run the auto-complete predicates. A predicate only ever
  closes a task, or reopens one *it* closed when the data behind it goes away (an offer
  withdrawn). A task a person closed stays closed.
- `complete` / `skip` / `reopen` are the manual overrides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.leads.models import Lead

from . import post_trip
from .definitions import KINDS, PREDECESSOR_KINDS, REGISTRY, AfterOpen, Level, TaskKind
from .facts import LeadFacts, load_facts
from .models import Task, TaskConfig

logger = logging.getLogger(__name__)


class TaskError(Exception):
    """A manual action the task can't take."""


def _anchor(kind: TaskKind, facts: LeadFacts, reservation) -> datetime | None:
    if kind.level == Level.TRIP:
        return reservation.pickup_at if reservation is not None else None
    return facts.first_pickup_at


def schedule(kind: TaskKind, facts: LeadFacts, reservation, *, now: datetime, opened_at=None):
    """(opens_at, due_at) for a task of `kind`. Opens now unless the kind opens later; a
    due date already behind the open time (a short-notice booking) is pulled up to it, so
    a task is never born overdue by days. `opened_at` pins the open time of a task that
    has already opened — rescheduling moves its due date, not when it opened."""
    anchor = _anchor(kind, facts, reservation)
    opens_at = now
    if opened_at is not None:
        opens_at = opened_at
    elif kind.opens is not None:
        opens_at = max(kind.opens.resolve(anchor) or now, now)
    if isinstance(kind.due, AfterOpen):
        due_at = kind.due.resolve_from(opens_at)
    else:
        due_at = kind.due.resolve(anchor)
    if due_at is not None and due_at < opens_at:
        due_at = opens_at
    return opens_at, due_at


def _predecessors_closed(kind: TaskKind, trip_id, statuses: dict) -> bool:
    """Every existing task of `kind.opens_after` on this trip is closed, and one exists."""
    found = [statuses[(trip_id, k)] for k in kind.opens_after if (trip_id, k) in statuses]
    return bool(found) and all(s in Task.CLOSED for s in found)


def _trip_wants(kind: TaskKind, facts: LeadFacts, trip, statuses: dict) -> bool:
    if kind.opens_after:
        if not _predecessors_closed(kind, trip.pk, statuses):
            return False
    elif kind.post_trip:
        if trip.pk not in facts.post_trip_ids:
            return False
    elif trip.pk in facts.ended_trip_ids:
        # A finished trip has nothing left to dispatch; don't raise pre-trip work for it.
        return False
    return kind.applies(facts, trip)


def _post_trip_state(facts: LeadFacts, config: TaskConfig, now: datetime) -> None:
    grace = timedelta(hours=config.post_trip_grace_hours)
    for trip in facts.live_trips:
        if post_trip.has_ended(trip, now=now, grace=grace):
            facts.ended_trip_ids.add(trip.pk)
            if post_trip.entered(trip, now=now, grace=grace):
                facts.post_trip_ids.add(trip.pk)


def _create_missing(lead: Lead, config: TaskConfig, now: datetime):
    """One locked pass: create every task that's wanted and missing. Returns (rows, facts),
    or (None, None) when the order isn't booked."""
    with transaction.atomic():
        # Locked AND re-read: the caller's instance may predate the change that brought
        # us here (a queryset .update(), another request).
        lead = Lead.objects.select_for_update().get(pk=lead.pk)
        if lead.status != Lead.Status.BOOKED:
            return None, None
        facts = load_facts([lead])[lead.pk]
        _post_trip_state(facts, config, now)
        statuses = {
            (res_id, kind): status
            for res_id, kind, status in Task.objects.filter(lead=lead).values_list(
                "reservation_id", "kind", "status"
            )
        }
        new = []
        # Registry order matters: a row created this pass is unresolved, so a later kind
        # that waits on it (thank-you on affiliate paid) correctly holds off.
        for kind in REGISTRY:
            if kind.level == Level.ORDER:
                targets = [None] if kind.applies(facts, None) else []
            else:
                targets = [t for t in facts.live_trips if _trip_wants(kind, facts, t, statuses)]
            for trip in targets:
                key = (getattr(trip, "pk", None), kind.key)
                if key in statuses:
                    continue
                opens_at, due_at = schedule(kind, facts, trip, now=now)
                status = Task.Status.SCHEDULED if opens_at > now else Task.Status.OPEN
                statuses[key] = status
                new.append(
                    Task(
                        kind=kind.key,
                        lead=lead,
                        reservation=trip,
                        department=kind.department,
                        assignee=config.owner_for(kind.department),
                        status=status,
                        opens_at=opens_at,
                        due_at=due_at,
                    )
                )
        Task.objects.bulk_create(new)
    return new, facts


def ensure_tasks(lead: Lead) -> list[Task]:
    """Create the missing tasks for a booked order, then evaluate its open ones.

    No-op for an unbooked order (tasks start at booking) or with tasks switched off.
    Post-trip stages open here too: once a trip has entered post-trip its `ops_review`
    is created, and each later stage once its `opens_after` predecessors are closed.
    Returns the rows it created.
    """
    config = TaskConfig.load()
    if not config.enabled:
        return []
    now = timezone.now()
    created: list[Task] = []
    # Another pass only when evaluation just closed a stage something waits on.
    for _ in range(len(REGISTRY)):
        new, facts = _create_missing(lead, config, now)
        if new is None:
            return created
        created += new
        changed = _evaluate_many(Task.objects.filter(lead_id=lead.pk), {lead.pk: facts})
        if not any(t.kind in PREDECESSOR_KINDS and t.is_closed for t in changed):
            break
    return created


def sync(lead: Lead) -> list[Task]:
    """The seam hook: ensure + evaluate. Used where a failure must never break the caller
    (money and booking paths) — `run-tasks` re-evaluates every open task, so a missed
    hook heals on the next tick rather than being lost."""
    try:
        return ensure_tasks(lead)
    except Exception:  # noqa: BLE001 - a task problem must never fail a payment or booking
        logger.exception("Task sync failed for lead %s", lead.pk)
        return []


def _evaluate_many(tasks, facts_by_lead: dict[int, LeadFacts]) -> list[Task]:
    """Apply predicates to `tasks` (open, scheduled, or system-closed). Returns the rows
    it changed."""
    now = timezone.now()
    changed = []
    for task in tasks:
        kind = KINDS.get(task.kind)
        facts = facts_by_lead.get(task.lead_id)
        if kind is None or facts is None:
            continue
        if (
            kind.not_applicable is not None
            and task.status in Task.UNRESOLVED
            and kind.not_applicable(task, facts)
        ):
            task.status = Task.Status.NOT_APPLICABLE
            task.completed_at = now
            task.completed_by = None
            changed.append(task)
            continue
        if kind.auto_complete is None:
            continue
        if task.status in Task.UNRESOLVED:
            if kind.auto_complete(task, facts):
                task.status = Task.Status.DONE
                task.completed_at = now
                task.completed_by = None
                changed.append(task)
        elif task.closed_by_system and not kind.auto_complete(task, facts):
            task.status = Task.Status.OPEN if task.opens_at <= now else Task.Status.SCHEDULED
            task.completed_at = None
            changed.append(task)
    for task in changed:
        task.updated_at = now
    if changed:
        Task.objects.bulk_update(changed, ["status", "completed_at", "completed_by", "updated_at"])
    return changed


def evaluate_lead(lead: Lead) -> int:
    fresh = Lead.objects.get(pk=lead.pk)
    return len(_evaluate_many(Task.objects.filter(lead=fresh), load_facts([fresh])))


def evaluate(task: Task) -> Task:
    _evaluate_many([task], load_facts([Lead.objects.get(pk=task.lead_id)]))
    return task


def reschedule(lead: Lead) -> int:
    """Re-date `lead`'s unresolved tasks after a pickup moved. Closed tasks keep their
    history; an already-open task keeps its open time; `escalated_tier` is left alone, so
    a task pushed back out of overdue won't re-alert for a tier it already raised."""
    lead = Lead.objects.get(pk=lead.pk)
    facts = load_facts([lead])[lead.pk]
    trips = {t.pk: t for t in facts.trips}
    now = timezone.now()
    changed = []
    for task in Task.objects.filter(lead=lead, status__in=Task.UNRESOLVED):
        kind = KINDS.get(task.kind)
        if kind is None:
            continue
        opened = task.opens_at if task.status == Task.Status.OPEN else None
        opens_at, due_at = schedule(
            kind, facts, trips.get(task.reservation_id), now=now, opened_at=opened
        )
        if (opens_at, due_at) != (task.opens_at, task.due_at):
            task.opens_at, task.due_at, task.updated_at = opens_at, due_at, now
            if task.status == Task.Status.SCHEDULED and opens_at <= now:
                task.status = Task.Status.OPEN
            changed.append(task)
    Task.objects.bulk_update(changed, ["opens_at", "due_at", "status", "updated_at"])
    return len(changed)


def _mark_not_applicable(tasks) -> int:
    return tasks.filter(status__in=Task.UNRESOLVED).update(
        status=Task.Status.NOT_APPLICABLE,
        completed_at=timezone.now(),
        completed_by=None,
        updated_at=timezone.now(),
    )


def cancel_for_trips(reservations) -> int:
    """Trips stopped needing work (cancelled, or about to be deleted). Their unresolved
    tasks become not-applicable; if that leaves an order with no live trips, its
    order-level tasks go too. Called from `dispatch.services.release_trips`, so coverage
    and tasks are released through the same door."""
    ids = [r.pk for r in reservations]
    if not ids:
        return 0
    count = _mark_not_applicable(Task.objects.filter(reservation_id__in=ids))
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.reservations.models import Reservation

    lead_ids = set(Reservation.objects.filter(pk__in=ids).values_list("lead_id", flat=True))
    live = set(
        Reservation.objects.filter(lead_id__in=lead_ids)
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .values_list("lead_id", flat=True)
    )
    dead = lead_ids - live
    if dead:
        count += _mark_not_applicable(
            Task.objects.filter(lead_id__in=dead, reservation__isnull=True)
        )
    return count


def complete(task: Task, user=None, note: str = "") -> Task:
    if task.status == Task.Status.NOT_APPLICABLE:
        raise TaskError("This task no longer applies.")
    task.status = Task.Status.DONE
    task.completed_at = timezone.now()
    task.completed_by = user
    fields = ["status", "completed_at", "completed_by", "updated_at"]
    if note.strip():
        task.note = note.strip()
        fields.append("note")
    task.save(update_fields=fields)
    _advance(task)
    return task


def _advance(task: Task) -> None:
    """Closing a stage opens the next one now, not on the next tick."""
    if task.kind in PREDECESSOR_KINDS:
        sync(Lead(pk=task.lead_id))


def skip(task: Task, user, note: str) -> Task:
    if not (note or "").strip():
        raise TaskError("Say why you're skipping this task.")
    if task.status == Task.Status.NOT_APPLICABLE:
        raise TaskError("This task no longer applies.")
    task.status = Task.Status.SKIPPED
    task.completed_at = timezone.now()
    task.completed_by = user
    task.note = note.strip()
    task.save(update_fields=["status", "completed_at", "completed_by", "note", "updated_at"])
    _advance(task)
    return task


def reopen(task: Task, user) -> Task:
    task.status = Task.Status.OPEN if task.opens_at <= timezone.now() else Task.Status.SCHEDULED
    task.completed_at = None
    task.completed_by = None
    task.escalated_tier = 0
    task.save(
        update_fields=["status", "completed_at", "completed_by", "escalated_tier", "updated_at"]
    )
    return task


def reassign(task: Task, assignee) -> Task:
    """Hand the task to `assignee` (None = unassigned). The escalation tier is kept: a
    reset would re-send tier 2 to every admin, and the new owner sees it in their queue."""
    task.assignee = assignee
    task.save(update_fields=["assignee", "updated_at"])
    return task
