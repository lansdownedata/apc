"""Task row actions (APC-53) — JSON POSTs shared by the queue and the checklists (A6).

Departments route work but grant nothing (D1), so any signed-in staff member may act on
any task; `completed_by` records who did.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from apps.accounts.models import User

from . import services
from .models import Task
from .queue import row_json


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
