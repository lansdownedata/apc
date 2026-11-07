"""Desk actions on a pending farm-in request: price, vehicle, coverage, send back, decide.

Every action locks the request row first (`_locked`), then checks it is still pending, then
acts, so two staff deciding at once cannot both win. Callers see `FarmInActionError` with an
HTTP-ish `status`: 409 when someone already decided, 400 for any other refusal.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Department, User
from apps.dispatch import farm_in, farm_in_push
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.leads.models import VehicleType
from apps.leads.services import BookLeadError, book_lead
from apps.notifications.models import Notification

logger = logging.getLogger(__name__)

_DECIDED = (FarmInRequest.Status.ACCEPTED, FarmInRequest.Status.DECLINED)
_TEXT_MAX = 255


class FarmInActionError(Exception):
    """A desk action was refused; `status` is the HTTP status the view should answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _locked(pk: int) -> FarmInRequest:
    """Fetch the request under a row lock; call inside `transaction.atomic()`.

    `of=("self",)` locks only the request row: Postgres refuses a plain FOR UPDATE on the
    nullable side of the outer join that `vehicle` brings in.
    """
    return (
        FarmInRequest.objects.select_for_update(of=("self",))
        .select_related("company", "vehicle")
        .get(pk=pk)
    )


def _require_pending(req: FarmInRequest) -> None:
    if req.status in _DECIDED:
        raise FarmInActionError("Already decided", status=409)
    if req.status != FarmInRequest.Status.PENDING:
        raise FarmInActionError(f"Request is {req.get_status_display().lower()}")


def _notify_quietly(
    kind: str, title: str, detail: str, req_pk: int, user: User | None = None
) -> None:
    """Create one notification; a failure is logged, never raised into the action."""
    try:
        Notification.objects.create(
            lead=None,
            kind=kind,
            title=title[:160],
            detail=detail[:_TEXT_MAX],
            user=user,
            farm_in_request_id=req_pk,
        )
    except Exception:
        logger.exception("farm-in: %s notification failed for request %s", kind, req_pk)


def price(pk: int, *, user: User, amount: Decimal, note: str = "") -> FarmInRequest:
    """Set a manual price; it survives later vehicle changes and requester updates."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if amount is None or amount <= 0:
            raise FarmInActionError("Enter a price above zero")
        req.quoted_amount = amount
        req.pricing_source = FarmInRequest.PricingSource.MANUAL
        req.priced_by = user
        req.pricing_note = note[:_TEXT_MAX]
        req.save()
    return req


def set_vehicle(pk: int, *, user: User, vehicle_id: int) -> FarmInRequest:
    """Map the request to one of our vehicle types and re-price it unless priced by hand."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        vehicle = VehicleType.objects.filter(pk=vehicle_id, active=True).first()
        if vehicle is None:
            raise FarmInActionError("Unknown vehicle")
        req.vehicle = vehicle
        if req.pricing_source != FarmInRequest.PricingSource.MANUAL:
            req.quoted_amount = farm_in.price_request(
                vehicle, hourly=farm_in.is_hourly(req.payload)
            )
            req.pricing_source = (
                FarmInRequest.PricingSource.RATE_CARD if req.quoted_amount is not None else ""
            )
        farm_in._remove_alert(req, farm_in.ALERT_UNMAPPED_PREFIX)
        req.save()
    return req


def request_coverage(pk: int, *, user: User) -> FarmInRequest:
    """Hand the request to dispatch and tell the operations team."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if req.desk != FarmInRequest.Desk.RESERVATIONS:
            raise FarmInActionError("Already with dispatch")
        req.desk = FarmInRequest.Desk.DISPATCH
        req.desk_changed_by = user
        req.desk_changed_at = timezone.now()
        req.save()
        title = f"Coverage requested: {req.company.name}"
        detail = f"{req.pickup_summary} → {req.dropoff_summary}"
        recipients = list(User.objects.in_department(Department.OPERATIONS).filter(is_active=True))
        recipients = recipients or [None]
        for recipient in recipients:
            transaction.on_commit(
                lambda r=recipient: _notify_quietly(
                    Notification.Kind.FARM_IN_COVERAGE, title, detail, pk, r
                )
            )
    return req


def send_back(pk: int, *, user: User, note: str) -> FarmInRequest:
    """Return the request to reservations with a reason, telling whoever asked for coverage."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if req.desk != FarmInRequest.Desk.DISPATCH:
            raise FarmInActionError("Already with reservations")
        note = note.strip()
        if not note:
            raise FarmInActionError("Say why it is going back")
        previous = req.desk_changed_by
        if previous is not None and not previous.is_active:
            previous = None
        req.desk = FarmInRequest.Desk.RESERVATIONS
        req.desk_note = note[:_TEXT_MAX]
        req.desk_changed_by = user
        req.desk_changed_at = timezone.now()
        req.save()
        title = f"Sent back: {req.company.name}"
        transaction.on_commit(
            lambda: _notify_quietly(
                Notification.Kind.FARM_IN_SENT_BACK, title, note[:_TEXT_MAX], pk, previous
            )
        )
    return req


def decline(pk: int, *, user: User, reason: str) -> FarmInRequest:
    """Decline the request and queue the REJECT push to GNet."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        reason = reason.strip()
        if not reason:
            raise FarmInActionError("Give the requester a reason")
        req.status = FarmInRequest.Status.DECLINED
        req.decline_reason = reason[:_TEXT_MAX]
        req.decided_by = user
        req.decided_at = timezone.now()
        req.save()
        farm_in_push.enqueue(req, "REJECT")
    return req


ALERT_ACCEPTED_NO_PICKUP = "No pickup time: set it on the order"


def accept(pk: int, *, user: User) -> FarmInRequest:
    """Book the request as an order and queue the CONFIRMED push with the trip's total.

    Refused without `GNET_API_KEY`: the push would fail at once and GNet, never hearing
    back, would time the request out and cover the trip elsewhere too.

    The order already exists from arrival; it is booked inside the same transaction that
    holds the request lock, so a refused booking leaves the request pending. Alerts the
    booking resolves (unmapped vehicle, missing pickup) are dropped; the rest stay.
    """
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if not settings.GNET_API_KEY:
            raise FarmInActionError("GNet pushes are not configured: set GNET_API_KEY")
        if req.quoted_amount is None:
            raise FarmInActionError("Price the request first")
        if req.vehicle is None:
            raise FarmInActionError("Choose a vehicle first")
        reservation = req.reservation
        try:
            book_lead(req.lead)
        except BookLeadError as exc:
            raise FarmInActionError(str(exc)) from exc
        req.status = FarmInRequest.Status.ACCEPTED
        req.decided_by = user
        req.decided_at = timezone.now()
        for resolved in (farm_in.ALERT_UNMAPPED_PREFIX, farm_in.ALERT_NO_PICKUP):
            farm_in._remove_alert(req, resolved)
        if req.pickup_at is None:
            farm_in._add_alert(req, ALERT_ACCEPTED_NO_PICKUP)
        req.save()
        farm_in_push.enqueue(req, "CONFIRMED", reservation.line_total)
    return req


def retry_push(push_pk: int, *, user: User) -> FarmInStatusPush:
    """Queue a fresh push for a failed one; refuses when delivered or already queued."""
    with transaction.atomic():
        push = FarmInStatusPush.objects.get(pk=push_pk)
        _locked(push.request_id)
        push.refresh_from_db()
        try:
            return farm_in_push.retry(push)
        except ValueError as exc:
            raise FarmInActionError(str(exc)) from exc
