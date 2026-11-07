"""Desk actions on a pending farm-in request: coverage, send back, decide.

Every action locks the request row first (`_locked`), then checks it is still pending, then
acts, so two staff deciding at once cannot both win. Callers see `FarmInActionError` with an
HTTP-ish `status`: 409 when someone already decided, 400 for any other refusal.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Department, User
from apps.dispatch import farm_in, farm_in_push
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.leads.models import Lead
from apps.leads.services import BookLeadError, LeadStateError, book_lead, mark_lost
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
    nullable side of an outer join.
    """
    return (
        FarmInRequest.objects.select_for_update(of=("self",))
        .select_related("company", "lead", "reservation")
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
        detail = farm_in._route(req)
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
    """Decline the request, mark its lead Lost with the reason and queue the REJECT push.

    A lead that is already Lost stays as it is; a Booked one refuses before anything changes.
    """
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        reason = reason.strip()
        if not reason:
            raise FarmInActionError("Give the requester a reason")
        if req.lead.status == Lead.Status.BOOKED:
            raise FarmInActionError("Lead is booked")
        try:
            mark_lost(req.lead, reason)
        except LeadStateError:
            pass
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
        reservation = req.reservation
        if reservation.vehicle_id is None or reservation.line_total <= 0:
            raise FarmInActionError("Price the trip first")
        if req.lead.reservations.count() > 1:
            raise FarmInActionError("A GNet request is one trip; remove the extra trips first")
        # Already Booked is fine: `book_lead` is idempotent, and GNet still needs CONFIRMED.
        if req.lead.status != Lead.Status.BOOKED and not req.lead.can_transition(
            Lead.Status.BOOKED
        ):
            raise FarmInActionError(f"Lead is {req.lead.get_status_display().lower()}")
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
