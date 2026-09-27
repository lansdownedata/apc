"""Overdue-task escalation (APC-52) — called from `run-tasks` every tick.

Reuses the APC-23 delivery: tray `Notification` rows plus an HTML digest email through
`send_html_email`. No second channel, and no SMS (dispatch keeps its own SMS tier).

- **Tier 1**, once overdue: the assignee — or the department's default owner when
  unassigned, or every admin when there's no owner either.
- **Tier 2**, overdue past `TaskConfig.overdue_grace_hours`: the department owner and
  every admin.
- `Task.escalated_tier` records what was sent, so a re-tick never repeats a tier (the
  `DispatchException.notified_tier` trick). A task re-dated out of overdue keeps its tier.
- Kinds with `escalates=False` — the checkpoints `monitor-dispatch` already alerts on —
  never escalate here (decision D2). They still show as tasks.
- **Digest**: once per business day, from `DIGEST_HOUR` local, to the configured list.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Department, User
from apps.leads.models import Lead
from apps.notifications.email import send_html_email
from apps.notifications.models import Notification
from apps.vendors.models import Vendor

from .definitions import KINDS
from .models import Task, TaskConfig

log = logging.getLogger(__name__)

# The digest's earliest send time, business-local. Every-15-minutes cron + a date stamp
# means it goes out on the first tick after this hour, once a day.
DIGEST_HOUR = 7

_ESCALATING = [k.key for k in KINDS.values() if k.escalates]


def _overdue(now):
    return (
        Task.objects.filter(status=Task.Status.OPEN, due_at__lt=now, kind__in=_ESCALATING)
        .filter(Q(lead__status=Lead.Status.BOOKED) | Q(vendor__status=Vendor.Status.ACTIVE))
        .with_order_tz()
        .select_related("lead__contact", "reservation", "assignee", "vendor", "insurance")
        .order_by("due_at", "pk")
    )


def _detail(task: Task) -> str:
    if task.vendor_id:
        policy = f" · {task.insurance}" if task.insurance_id else ""
        due = f" · due {task.due_display}" if task.due_at else ""
        return f"{task.vendor.name}{policy}{due}"[:255]
    trip = f" · trip #{task.reservation_id}" if task.reservation_id else ""
    due = f" · due {task.due_display}" if task.due_at else ""
    return f"{task.lead.quote_no} · {task.lead.contact.name}{trip}{due}"[:255]


def run(config: TaskConfig, now) -> int:
    tasks = list(_overdue(now))
    if not tasks:
        return 0
    admins = list(User.objects.filter(role=User.Role.OWNER_ADMIN, is_active=True).order_by("pk"))
    owners = {d: config.owner_for(d) for d in Department.values}
    grace = timedelta(hours=config.overdue_grace_hours)

    notes: list[Notification] = []
    changed: list[Task] = []
    for task in tasks:
        owner = owners.get(task.department)
        tier = task.escalated_tier
        if tier < 1:
            first = task.assignee or owner
            notes += _notes(task, [first] if first else admins, "Overdue")
            tier = 1
        if tier < 2 and task.due_at + grace < now:
            notes += _notes(task, [owner, *admins], "Escalated")
            tier = 2
        if tier != task.escalated_tier:
            task.escalated_tier = tier
            task.updated_at = now
            changed.append(task)
    Notification.objects.bulk_create(notes)
    Task.objects.bulk_update(changed, ["escalated_tier", "updated_at"])

    _maybe_send_digest(config, tasks, now)
    return len(changed)


def _notes(task: Task, recipients, prefix: str) -> list[Notification]:
    seen, out = set(), []
    for user in recipients:
        if user is None or user.pk in seen:
            continue
        seen.add(user.pk)
        out.append(
            Notification(
                lead=task.lead,
                vendor=task.vendor,
                user=user,
                kind=Notification.Kind.TASK_OVERDUE,
                title=f"{prefix}: {task.label}"[:160],
                detail=_detail(task),
            )
        )
    return out


def _maybe_send_digest(config: TaskConfig, tasks: list[Task], now) -> bool:
    local_now = timezone.localtime(now)
    today = local_now.date()
    if local_now.hour < DIGEST_HOUR or config.digest_sent_on == today:
        return False

    groups: dict[str, list[dict]] = defaultdict(list)
    for task in tasks:  # already oldest-due first
        groups[task.department].append(
            {
                "label": task.label,
                # A vendor task names the affiliate and policy where an order would be.
                "quote_no": task.vendor.name if task.vendor_id else task.lead.quote_no,
                "customer": str(task.insurance or "") if task.vendor_id else task.lead.contact.name,
                "trip_id": task.reservation_id,
                "due": task.due_display,
                "assignee": task.assignee,
            }
        )
    labels = dict(Department.choices)
    n = len(tasks)
    context = {
        "groups": [(labels[d], groups[d]) for d in Department.values if d in groups],
        "count": n,
        "now": local_now,
        "company_name": settings.COMPANY_NAME,
        "queue_url": f"{settings.PUBLIC_BASE_URL}/portal/tasks/"
        if settings.PUBLIC_BASE_URL
        else "",
    }
    subject = f"{n} overdue task{'s' if n != 1 else ''}"
    for recipient in config.digest_list:
        send_html_email(to=recipient, subject=subject, template="task_digest", context=context)
    TaskConfig.objects.filter(pk=config.pk).update(digest_sent_on=today)
    config.digest_sent_on = today
    log.info("task digest: %d overdue task(s) sent for %s", n, today)
    return True
