"""Vendor-level tasks (APC-71) — affiliate insurance renewal as owned work.

`VendorInsurance` already grades each policy (valid → expiring 30d → … → expired), but
only on the vendor page. This turns each policy into an `insurance_renewal` task for
Affiliate Management: scheduled at creation, opening 30 days before expiry, due the
morning it expires, and escalating through APC-52 like any other task.

- Every policy of an active vendor gets its own task (TASK-10). A later policy on file
  doesn't suppress or close it: a vendor with any lapsed policy can't operate, which is
  what Compliance and the vendor page show.
- The task follows its policy's expiry. Moved out of the window (renewed), an open task
  closes itself (system-done) and comes back when the new expiry's window opens; moved
  inside it, the due date follows. Removing the policy (a delete — there is no inactive
  flag) deletes its task with it.
- Deactivating the vendor marks its unresolved tasks not-applicable.

`run` is called from `run-tasks` for every vendor, and `sync_vendor` from the vendor
screens (insurance add/edit, vendor edit) so a change shows at once. A fixed handful of
queries per run, however many vendors.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

from apps.vendors.models import Vendor, VendorInsurance

from . import schedule
from .definitions import KINDS, apply_schedule
from .models import Task, TaskConfig

KIND = "insurance_renewal"


def _follow(task: Task, policy: VendorInsurance, kind, now: datetime) -> bool:
    """Bring an existing task in line with its policy's expiry. Returns whether it changed.

    The open time never moves on a task that has opened, the escalation tier only resets
    when a closed task comes back for a new expiry, and a task a person closed stays closed.
    """
    opens = kind.opens.resolve(policy.expiry_date)
    due = kind.due.resolve(policy.expiry_date)
    before = (task.status, task.opens_at, task.due_at)
    if task.status == Task.Status.OPEN:
        if opens > now:  # renewed: its window is in the future again
            task.status, task.completed_at, task.completed_by = Task.Status.DONE, now, None
        else:
            task.due_at = max(due, task.opens_at)
    elif task.status == Task.Status.SCHEDULED:
        task.opens_at = max(opens, now)
        task.due_at = max(due, task.opens_at)
        if task.opens_at <= now:
            task.status = Task.Status.OPEN
    elif task.closed_by_system and opens <= now:
        # Its window has come round again (or the old "a later policy covers it" rule
        # closed it while the policy still lapses).
        task.status, task.completed_at, task.escalated_tier = Task.Status.OPEN, None, 0
        task.opens_at = max(opens, task.opens_at)
        task.due_at = max(due, task.opens_at)
    else:
        return False
    if (task.status, task.opens_at, task.due_at) == before:
        return False
    task.updated_at = now
    return True


def run(
    now: datetime | None = None, *, vendor_id: int | None = None, config: TaskConfig | None = None
) -> int:
    """Create, follow, close and retire renewal tasks. Idempotent; returns the rows changed."""
    config = config or TaskConfig.load()
    if not config.enabled:
        return 0
    now = now or timezone.now()
    kind = KINDS[KIND]
    if row := schedule.load().get((KIND, "standard")):
        kind = apply_schedule(kind, *row)
    policies = VendorInsurance.objects.filter(vendor__status=Vendor.Status.ACTIVE)
    tasks = Task.objects.filter(kind=KIND)
    if vendor_id is not None:
        policies = policies.filter(vendor_id=vendor_id)
        tasks = tasks.filter(vendor_id=vendor_id)
    policies = list(policies)
    have = {t.insurance_id: t for t in tasks.filter(insurance__in=policies)}
    owner = config.owner_for(kind.department)

    new, followed = [], []
    for policy in policies:
        task = have.get(policy.pk)
        if task is not None:
            if _follow(task, policy, kind, now):
                followed.append(task)
            continue
        opens_at = max(kind.opens.resolve(policy.expiry_date), now)
        new.append(
            Task(
                kind=KIND,
                vendor_id=policy.vendor_id,
                insurance=policy,
                department=kind.department,
                assignee=owner,
                status=Task.Status.SCHEDULED if opens_at > now else Task.Status.OPEN,
                opens_at=opens_at,
                due_at=max(kind.due.resolve(policy.expiry_date), opens_at),
            )
        )
    Task.objects.bulk_create(new)
    Task.objects.bulk_update(
        followed,
        [
            "status",
            "opens_at",
            "due_at",
            "completed_at",
            "completed_by",
            "escalated_tier",
            "updated_at",
        ],
    )

    changed = len(new) + len(followed)
    changed += (
        tasks.filter(status__in=Task.UNRESOLVED)
        .exclude(vendor__status=Vendor.Status.ACTIVE)
        .update(
            status=Task.Status.NOT_APPLICABLE, completed_at=now, completed_by=None, updated_at=now
        )
    )
    return changed


def redate(now: datetime | None = None) -> int:
    """Recompute `due_at` of unresolved renewal tasks after the schedule changed. The open
    time and escalation tier stay put; due never lands before the task opened."""
    now = now or timezone.now()
    kind = KINDS[KIND]
    if row := schedule.load().get((KIND, "standard")):
        kind = apply_schedule(kind, *row)
    changed = []
    tasks = Task.objects.filter(
        kind=KIND, status__in=Task.UNRESOLVED, insurance__isnull=False
    ).select_related("insurance")
    for task in tasks:
        due_at = max(kind.due.resolve(task.insurance.expiry_date), task.opens_at)
        if due_at != task.due_at:
            task.due_at, task.updated_at = due_at, now
            changed.append(task)
    Task.objects.bulk_update(changed, ["due_at", "updated_at"])
    return len(changed)


def sync_vendor(vendor: Vendor) -> int:
    """The hook for the vendor screens — the same pass, for one vendor."""
    return run(vendor_id=vendor.pk)
