"""The task queue (APC-53) — what a staff member owes, worst first.

One query per page, however many rows: every column is `select_related` or an
annotation, including the order-level rows' "pickup" (their order's first trip, by
subquery). No row ever touches `Reservation.pickup`, which bypasses prefetch.

Windows ("overdue", "today", the badge) are measured in the business timezone — they're
about the viewer's working day. The *displayed* times are always the trip's own zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import BooleanField, Case, F, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from apps.accounts.models import Department

from .definitions import KINDS, Level
from .models import Task, format_local

TRIP_KIND_KEYS = frozenset(k.key for k in KINDS.values() if k.level == Level.TRIP)
DUE_WINDOWS = ("overdue", "today", "week")


def end_of_local_day(days_ahead: int = 0) -> datetime:
    day = timezone.localdate() + timedelta(days=days_ahead + 1)
    return datetime.combine(day, time(0, 0), tzinfo=timezone.get_current_timezone())


def _start_of_local_day() -> datetime:
    return end_of_local_day(-1)


@dataclass(frozen=True)
class QueueFilters:
    department: str = ""
    # "me" (default) · "anyone" · "unassigned" · a user pk as a string
    assignee: str = "me"
    due: str = ""
    kind: str = ""

    @classmethod
    def from_query(cls, params) -> QueueFilters:
        """Parse GET params, dropping anything that isn't a real choice."""
        department = params.get("department", "")
        assignee = params.get("assignee", "") or "me"
        due = params.get("due", "")
        kind = params.get("kind", "")
        if assignee not in ("me", "anyone", "unassigned") and not assignee.isdigit():
            assignee = "me"
        return cls(
            department=department if department in Department.values else "",
            assignee=assignee,
            due=due if due in DUE_WINDOWS else "",
            kind=kind if kind in KINDS else "",
        )


def _first_trip(field: str):
    from apps.reservations.models import Reservation

    first = Reservation.objects.filter(lead_id=OuterRef("lead_id")).order_by(
        "pickup_date", "pickup_time", "pk"
    )
    return Subquery(first.values(field)[:1])


def queue_for(user, filters: QueueFilters):
    """Open tasks for `filters`, overdue first, then soonest due; undated last."""
    now = timezone.now()
    qs = (
        Task.objects.filter(status=Task.Status.OPEN)
        .select_related("lead__contact", "reservation", "assignee", "vendor", "insurance")
        .with_order_tz()
        .annotate(
            order_pickup_date=_first_trip("pickup_date"),
            order_pickup_time=_first_trip("pickup_time"),
            is_overdue=Case(
                When(due_at__lt=now, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )
    )
    if filters.assignee == "me":
        qs = qs.filter(assignee=user)
    elif filters.assignee == "unassigned":
        qs = qs.filter(assignee__isnull=True)
    elif filters.assignee.isdigit():
        qs = qs.filter(assignee_id=int(filters.assignee))
    if filters.department:
        qs = qs.filter(department=filters.department)
    if filters.kind:
        qs = qs.filter(kind=filters.kind)
    if filters.due == "overdue":
        qs = qs.filter(due_at__lt=now)
    elif filters.due == "today":
        qs = qs.filter(due_at__gte=_start_of_local_day(), due_at__lt=end_of_local_day())
    elif filters.due == "week":
        qs = qs.filter(due_at__gte=now, due_at__lt=end_of_local_day(7))
    return qs.order_by("-is_overdue", F("due_at").asc(nulls_last=True), "pk")


def badge_count(user) -> int:
    """My overdue + due-today open tasks — the nav badge. One COUNT query."""
    if not getattr(user, "is_authenticated", False):
        return 0
    return Task.objects.filter(
        Q(status=Task.Status.OPEN), assignee=user, due_at__lt=end_of_local_day()
    ).count()


def row_json(task: Task) -> dict:
    """What the row actions return so the row (queue or checklist) updates in place."""
    who = task.completed_by
    return {
        "id": task.pk,
        "status": task.status,
        "status_label": task.get_status_display(),
        "assignee": (task.assignee.get_full_name() or task.assignee.username)
        if task.assignee
        else "",
        "assignee_id": task.assignee_id,
        "completed_by": (who.get_full_name() or who.username) if who else "",
        "completed_on": format_local(task.local(task.completed_at)) if task.completed_at else "",
        "note": task.note,
        "auto": task.closed_by_system,
    }
