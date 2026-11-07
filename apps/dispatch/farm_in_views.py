"""Staff endpoints for the farm-in desk actions (APC-135); the services own the rules."""

from collections.abc import Callable

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from . import farm_in_actions
from .farm_in_actions import FarmInActionError
from .models import FarmInRequest, FarmInStatusPush


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _run(fn: Callable[..., FarmInRequest], *args, **kwargs) -> JsonResponse:
    """Call a desk action and answer JSON; refusals carry the service's status."""
    try:
        req = fn(*args, **kwargs)
    except FarmInActionError as exc:
        return _error(str(exc), exc.status)
    except FarmInRequest.DoesNotExist as exc:
        raise Http404 from exc
    return JsonResponse(
        {
            "ok": True,
            "status": req.status,
            "desk": req.desk,
            "lead": req.lead_id,
        }
    )


@login_required
@require_POST
def request_coverage(request: HttpRequest, pk: int) -> JsonResponse:
    return _run(farm_in_actions.request_coverage, pk, user=request.user)


@login_required
@require_POST
def send_back(request: HttpRequest, pk: int) -> JsonResponse:
    return _run(farm_in_actions.send_back, pk, user=request.user, note=request.POST.get("note", ""))


@login_required
@require_POST
def accept(request: HttpRequest, pk: int) -> JsonResponse:
    return _run(farm_in_actions.accept, pk, user=request.user)


@login_required
@require_POST
def decline(request: HttpRequest, pk: int) -> JsonResponse:
    return _run(
        farm_in_actions.decline, pk, user=request.user, reason=request.POST.get("reason", "")
    )


@login_required
@require_POST
def retry_push(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        push = farm_in_actions.retry_push(pk, user=request.user)
    except FarmInActionError as exc:
        return _error(str(exc), exc.status)
    except (FarmInStatusPush.DoesNotExist, FarmInRequest.DoesNotExist) as exc:
        raise Http404 from exc
    return JsonResponse({"ok": True, "push": push.pk})
