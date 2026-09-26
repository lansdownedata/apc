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
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.leads.models import Lead

from .definitions import KINDS, REGISTRY, AfterOpen, Level, TaskKind
from .facts import LeadFacts, load_facts
from .models import Task, TaskConfig

logger = logging.getLogger(__name__)


class TaskError(Exception):
    """A manual action the task can't take."""


def _anchor(kind: TaskKind, facts: LeadFacts, reservation) -> datetime | None:
    if kind.level == Level.TRIP:
        return reservation.pickup_at if reservation is not None else None
    return facts.first_pickup_at


def schedule(kind: TaskKind, facts: LeadFacts, reservation, *, now: datetime):
    """(opens_at, due_at) for a task of `kind`. Opens now unless the kind opens later; a
    due date already behind the open time (a short-notice booking) is pulled up to it, so
    a task is never born overdue by days."""
    anchor = _anchor(kind, facts, reservation)
    opens_at = now
    if kind.opens is not None:
        opens_at = max(kind.opens.resolve(anchor) or now, now)
    if isinstance(kind.due, AfterOpen):
        due_at = kind.due.resolve_from(opens_at)
    else:
        due_at = kind.due.resolve(anchor)
    if due_at is not None and due_at < opens_at:
        due_at = opens_at
    return opens_at, due_at


def _wanted(facts: LeadFacts) -> list[tuple[TaskKind, object]]:
    out = []
    for kind in REGISTRY:
        if kind.level == Level.ORDER:
            if kind.applies(facts, None):
                out.append((kind, None))
        else:
            out.extend((kind, trip) for trip in facts.live_trips if kind.applies(facts, trip))
    return out


def ensure_tasks(lead: Lead) -> list[Task]:
    """Create the missing tasks for a booked order, then evaluate its open ones.

    No-op for an unbooked order (tasks start at booking) or with tasks switched off.
    Returns the rows it created.
    """
    config = TaskConfig.load()
    if not config.enabled:
        return []
    now = timezone.now()
    with transaction.atomic():
        # Locked AND re-read: the caller's instance may predate the change that brought
        # us here (a queryset .update(), another request).
        lead = Lead.objects.select_for_update().get(pk=lead.pk)
        if lead.status != Lead.Status.BOOKED:
            return []
        facts = load_facts([lead])[lead.pk]
        existing = {
            (res_id, kind)
            for res_id, kind in Task.objects.filter(lead=lead).values_list("reservation_id", "kind")
        }
        new = []
        for kind, trip in _wanted(facts):
            if (getattr(trip, "pk", None), kind.key) in existing:
                continue
            opens_at, due_at = schedule(kind, facts, trip, now=now)
            new.append(
                Task(
                    kind=kind.key,
                    lead=lead,
                    reservation=trip,
                    department=kind.department,
                    assignee=config.owner_for(kind.department),
                    status=Task.Status.SCHEDULED if opens_at > now else Task.Status.OPEN,
                    opens_at=opens_at,
                    due_at=due_at,
                )
            )
        Task.objects.bulk_create(new)
    _evaluate_many(Task.objects.filter(lead=lead), {lead.pk: facts})
    return new


def sync(lead: Lead) -> None:
    """The seam hook: ensure + evaluate. Used where a failure must never break the caller
    (money and booking paths) — `run-tasks` re-evaluates every open task, so a missed
    hook heals on the next tick rather than being lost."""
    try:
        ensure_tasks(lead)
    except Exception:  # noqa: BLE001 - a task problem must never fail a payment or booking
        logger.exception("Task sync failed for lead %s", lead.pk)


def _evaluate_many(tasks, facts_by_lead: dict[int, LeadFacts]) -> int:
    """Apply predicates to `tasks` (open, scheduled, or system-closed). Returns changes."""
    now = timezone.now()
    changed = []
    for task in tasks:
        kind = KINDS.get(task.kind)
        facts = facts_by_lead.get(task.lead_id)
        if kind is None or kind.auto_complete is None or facts is None:
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
    return len(changed)


def evaluate_lead(lead: Lead) -> int:
    fresh = Lead.objects.get(pk=lead.pk)
    return _evaluate_many(Task.objects.filter(lead=fresh), load_facts([fresh]))


def evaluate(task: Task) -> Task:
    _evaluate_many([task], load_facts([Lead.objects.get(pk=task.lead_id)]))
    return task


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
    return task


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
