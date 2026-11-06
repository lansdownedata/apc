"""Staff endpoints for the farm-in desk actions (APC-135); the services own the rules."""

from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404, HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from . import farm_in_actions
from .farm_in_actions import FarmInActionError
from .models import FarmInRequest


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _run(fn: Callable[..., FarmInRequest], *args, **kwargs) -> JsonResponse:
    """Call a desk action and answer JSON; refusals carry the service's status."""
    try:
        req = fn(*args, **kwargs)
    except FarmInActionError as exc:
        return _error(str(exc), exc.status)
    except ObjectDoesNotExist as exc:
        raise Http404 from exc
    return JsonResponse(
        {
            "ok": True,
            "status": req.status,
            "desk": req.desk,
            "quoted_amount": None if req.quoted_amount is None else f"{req.quoted_amount:.2f}",
        }
    )


@login_required
@require_POST
def price(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        amount = Decimal(str(request.POST.get("amount", "")).strip())
    except (InvalidOperation, ValueError):
        return _error("Enter a valid amount")
    if not amount.is_finite():
        return _error("Enter a valid amount")
    return _run(
        farm_in_actions.price,
        pk,
        user=request.user,
        amount=amount,
        note=request.POST.get("note", ""),
    )


@login_required
@require_POST
def vehicle(request: HttpRequest, pk: int) -> JsonResponse:
    try:
        vehicle_id = int(str(request.POST.get("vehicle_id", "")).strip())
    except ValueError:
        return _error("Choose a vehicle")
    return _run(farm_in_actions.set_vehicle, pk, user=request.user, vehicle_id=vehicle_id)


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
    except ObjectDoesNotExist as exc:
        raise Http404 from exc
    return JsonResponse({"ok": True, "push": push.pk})
