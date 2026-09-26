"""Task row actions (APC-53) — JSON POSTs shared by the queue and the checklists (A6).

Departments route work but grant nothing (D1), so any signed-in staff member may act on
any task; `completed_by` records who did.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Department, User
from apps.leads.models import Lead
from apps.reservations.models import Reservation

from . import selectors, services
from .definitions import KIND_CHOICES
from .models import Task
from .queue import QueueFilters, end_of_local_day, queue_for, row_json

_ASSIGNEE_SCOPES = [("me", "Me"), ("anyone", "Anyone"), ("unassigned", "Unassigned")]
_DUE_WINDOWS = [("overdue", "Overdue"), ("today", "Due today"), ("week", "Next 7 days")]


def _team() -> list[tuple[int, str]]:
    users = User.objects.filter(is_active=True).order_by("first_name", "username")
    return [(u.pk, u.get_full_name() or u.username) for u in users]


@login_required
@require_GET
def task_queue(request: HttpRequest) -> HttpResponse:
    """What I owe, worst first (APC-53). The layout Moe signed off 2026-09-26."""
    filters = QueueFilters.from_query(request.GET)
    rows = list(queue_for(request.user, filters))
    end_of_today = end_of_local_day()
    team = _team()
    return render(
        request,
        "tasks/queue.html",
        {
            "nav": "tasks",
            "page_title": "Tasks",
            "rows": rows,
            "filters": filters,
            "overdue_count": sum(1 for r in rows if r.is_overdue),
            "today_count": sum(
                1 for r in rows if not r.is_overdue and r.due_at and r.due_at < end_of_today
            ),
            "department_options": Department.choices,
            "assignee_options": _ASSIGNEE_SCOPES + [(str(pk), name) for pk, name in team],
            "due_options": _DUE_WINDOWS,
            "kind_options": KIND_CHOICES,
            "team": team,
        },
    )


@login_required
@require_GET
def trip_checklist(request: HttpRequest, pk: int) -> HttpResponse:
    """One trip's checklist as a fragment — the trip-line icon's modal reloads from it."""
    trip = get_object_or_404(Reservation.objects.only("pk", "lead_id"), pk=pk)
    return render(
        request,
        "tasks/_checklist.html",
        {"tasks": selectors.checklist_for_trip(trip), "reload_url": request.path},
    )


@login_required
@require_GET
def order_checklist(request: HttpRequest, pk: int) -> HttpResponse:
    lead = get_object_or_404(Lead.objects.only("pk", "status"), pk=pk)
    return render(
        request,
        "tasks/_checklist.html",
        {
            "tasks": selectors.checklist_for_order(lead),
            "reload_url": reverse("order_checklist", args=[lead.pk]),
            "booked": lead.status == Lead.Status.BOOKED,
        },
    )


def _task(pk: int) -> Task:
    return get_object_or_404(Task.objects.select_related("assignee", "completed_by"), pk=pk)


def _ok(task: Task) -> JsonResponse:
    return JsonResponse({"ok": True, "task": row_json(task)})


def _bad(message: str) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=400)


@login_required
@require_POST
def task_complete(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        task = services.complete(_task(pk), user=request.user, note=request.POST.get("note", ""))
    except services.TaskError as exc:
        return _bad(str(exc))
    return _ok(task)


@login_required
@require_POST
def task_skip(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        task = services.skip(_task(pk), user=request.user, note=request.POST.get("note", ""))
    except services.TaskError as exc:
        return _bad(str(exc))
    return _ok(task)


@login_required
@require_POST
def task_reopen(request: HttpRequest, pk: int) -> JsonResponse:
    return _ok(services.reopen(_task(pk), user=request.user))


@login_required
@require_POST
def task_reassign(request: HttpRequest, pk: int) -> JsonResponse:
    raw = (request.POST.get("assignee") or "").strip()
    assignee = None
    if raw:
        assignee = User.objects.filter(pk=raw, is_active=True).first() if raw.isdigit() else None
        if assignee is None:
            return _bad("Choose someone on the team.")
    return _ok(services.reassign(_task(pk), assignee))
