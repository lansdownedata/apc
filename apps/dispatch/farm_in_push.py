"""Queue that delivers farm-in status pushes to the GNet gateway, with retries.

The gateway never retries anything on its side, so every `FarmInStatusPush` is retried
here: transport failures, 408/409/429 and 5xx back off exponentially (capped at 30 minutes)
up to `MAX_ATTEMPTS`; any other refusal fails at once and raises an alert for the desk.

A status is pushed once. A `sent` push blocks a repeat of the same status, except for the
`REPEATABLE` ones (a trip can be reassigned or re-canceled), and a `pending` push for a
status always blocks another.

Trip status changes (`after_trip_status`, called from `set_trip_status`) and a processed
review (`after_review_processed`, the final `CLOSE` with the trip's total) are what queue
pushes for an accepted request.
"""

import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.dispatch import farm_in
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.integrations import gnet
from apps.integrations.gnet import GnetAPIError
from apps.notifications.models import Notification

if TYPE_CHECKING:
    from apps.reservations.models import Reservation

logger = logging.getLogger(__name__)

REPEATABLE = frozenset({"ASSIGNED", "CANCEL"})
EXPIRED_ALERT = "Expired: pickup time passed"
EXPIRED_LOST_REASON = "GNet request expired"
EXPIRED_BOOKED_ALERT = "Expired, review the order"
MAX_ATTEMPTS = 8
RETRY_STATUSES = frozenset({0, 408, 409, 429})
EXPIRE_AFTER = timedelta(hours=1)
_MAX_BACKOFF = timedelta(minutes=30)
LEASE = timedelta(seconds=60)
_ALERT_PREFIX = "GNet "

# Our trip status -> the GNet status code we report. Any status not here reports nothing.
STATUS_MAP: dict[str, str] = {
    "assigned": "ASSIGNED",
    "dispatched": "ASSIGNED",
    "on_the_way": "EN_ROUTE",
    "arrived": "ON_LOCATION",
    "customer_in_car": "PASSENGER_ON_BOARD",
    "done": "COMPLETE",
    "cancelled": "CANCEL",
    "late_cancel": "CANCEL",
    "no_show": "NO_SHOW",
}


def _blocker(req: FarmInRequest, status: str) -> str:
    """Why a push for `status` may not be queued ("" when it may)."""
    existing = FarmInStatusPush.objects.filter(request=req, status=status)
    if existing.filter(state=FarmInStatusPush.State.PENDING).exists():
        return "A retry is already queued"
    if status not in REPEATABLE and existing.filter(state=FarmInStatusPush.State.SENT).exists():
        return "Already delivered"
    return ""


def _create(req: FarmInRequest, status: str, total_amount: Decimal | None) -> FarmInStatusPush:
    push = FarmInStatusPush.objects.create(
        request=req,
        status=status,
        total_amount=total_amount,
        state=FarmInStatusPush.State.PENDING,
        attempts=0,
        next_attempt_at=timezone.now(),
    )
    transaction.on_commit(lambda: _deliver_quietly(push.pk))
    return push


def enqueue(
    req: FarmInRequest, status: str, total_amount: Decimal | None = None
) -> FarmInStatusPush | None:
    """Queue a status push and try it once the transaction commits; None when blocked.

    The caller must hold `select_for_update` on `req`'s row for the whole transaction:
    the once-only check (`_blocker`) and the insert are only atomic under that lock.
    """
    if _blocker(req, status):
        return None
    return _create(req, status, total_amount)


def _enqueue_for_trip(
    reservation: "Reservation", status: str, total_amount: Decimal | None = None
) -> FarmInStatusPush | None:
    """Queue `status` for the trip's accepted farm-in request, if it has one.

    The request row is locked for the lookup and the enqueue, so two status changes landing
    together can't both pass `enqueue`'s once-only check. One query on an ordinary trip.
    """
    with transaction.atomic():
        req = (
            FarmInRequest.objects.select_for_update()
            .filter(reservation_id=reservation.pk, status=FarmInRequest.Status.ACCEPTED)
            .only("pk", "transaction_id")
            .first()
        )
        if req is None:
            return None
        return enqueue(req, status, total_amount)


def after_trip_status(reservation: "Reservation", status: str) -> FarmInStatusPush | None:
    """Report a trip status change to GNet; a status with no GNet code costs no query."""
    code = STATUS_MAP.get(status)
    if code is None:
        return None
    return _enqueue_for_trip(reservation, code)


def after_review_processed(trip: "Reservation") -> FarmInStatusPush | None:
    """Report a processed trip to GNet as `CLOSE` with its final total.

    Once a `CLOSE` is sent, unlocking and re-processing pushes nothing more (`enqueue`).
    """
    return _enqueue_for_trip(trip, "CLOSE", trip.line_total)


def final_reported(trip: "Reservation") -> Decimal | None:
    """The total GNet accepted as the trip's final (its sent `CLOSE`), or None."""
    return (
        FarmInStatusPush.objects.filter(
            request__reservation_id=trip.pk, status="CLOSE", state=FarmInStatusPush.State.SENT
        )
        .values_list("total_amount", flat=True)
        .first()
    )


def retry(push: FarmInStatusPush) -> FarmInStatusPush:
    """Queue a fresh push for a failed one; raises if the status was delivered or is queued.

    The once-only rule still holds: a failed row stays retryable, so a sibling that was
    already sent for a non-repeatable status must block a second retry.
    """
    if push.state != FarmInStatusPush.State.FAILED:
        raise ValueError("Only a failed push can be retried")
    reason = _blocker(push.request, push.status)
    if reason:
        raise ValueError(reason)
    return _create(push.request, push.status, push.total_amount)


def _failure_prefix(status: str) -> str:
    return f"{_ALERT_PREFIX}{status} not delivered"


def _fail(push: FarmInStatusPush, reason: str) -> FarmInStatusPush:
    """Mark a push terminally failed and tell the desk.

    The failure becomes one segment of the request's alert, replacing an older failure for
    the same status; the other segments stay.
    """
    push.state = FarmInStatusPush.State.FAILED
    push.last_error = reason[:500]
    push.save(update_fields=["state", "last_error", "attempts", "updated_at"])
    req = push.request
    req.refresh_from_db(fields=["alert"])
    farm_in._remove_alert(req, _failure_prefix(push.status))
    farm_in._add_alert(req, f"{_failure_prefix(push.status)}: {reason}")
    req.save(update_fields=["alert", "updated_at"])
    Notification.notify(
        None,
        Notification.Kind.FARM_IN_PUSH_FAILED,
        title=f"GNet {push.status} not delivered for {req.company.name}"[:160],
        detail=reason[:255],
        farm_in_request=req,
    )
    return push


def deliver(push: FarmInStatusPush) -> FarmInStatusPush:
    """Make one delivery attempt and record the outcome on the push.

    The attempt is claimed first by pushing `next_attempt_at` out by `LEASE`, so an
    overlapping cron run or the on-commit attempt skips a push already in flight. A push
    that is not pending, or not yet due, is returned untouched.
    """
    now = timezone.now()
    claimed = FarmInStatusPush.objects.filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
        pk=push.pk,
        state=FarmInStatusPush.State.PENDING,
    ).update(next_attempt_at=now + LEASE)
    if not claimed:
        return push
    if not settings.GNET_API_KEY:
        return _fail(push, "GNET_API_KEY not set")
    req = push.request
    try:
        gnet.push_status(req.transaction_id, push.status, push.total_amount)
    except GnetAPIError as exc:
        if exc.status not in RETRY_STATUSES and exc.status < 500:
            return _fail(push, str(exc))
        push.attempts += 1
        if push.attempts >= MAX_ATTEMPTS:
            return _fail(push, str(exc))
        delay = timedelta(seconds=2**push.attempts * 30 + random.randint(0, 5))
        push.next_attempt_at = timezone.now() + min(delay, _MAX_BACKOFF)
        push.last_error = str(exc)[:500]
        push.save(update_fields=["attempts", "next_attempt_at", "last_error", "updated_at"])
        return push
    push.state = FarmInStatusPush.State.SENT
    push.sent_at = timezone.now()
    push.last_error = ""
    push.save(update_fields=["state", "sent_at", "last_error", "updated_at"])
    req.refresh_from_db(fields=["alert"])
    before = req.alert
    farm_in._remove_alert(req, _failure_prefix(push.status))
    if req.alert != before:
        req.save(update_fields=["alert", "updated_at"])
    return push


def _deliver_quietly(push_pk: int) -> None:
    """Deliver after commit; a failure is logged, never raised into the caller's request."""
    try:
        push = FarmInStatusPush.objects.select_related("request__company").get(pk=push_pk)
        deliver(push)
    except Exception:
        logger.exception("farm-in: status push %s failed unexpectedly", push_pk)


def run_cron() -> int:
    """Attempt every due push, then expire pending requests an hour past pickup.

    Expiry sends nothing to GNet (the trip simply passed) and raises no notification; the
    request's lead is marked Lost.
    Returns pushes attempted plus requests whose expiry was attempted.
    """
    now = timezone.now()
    due = FarmInStatusPush.objects.filter(
        state=FarmInStatusPush.State.PENDING, next_attempt_at__lte=now
    ).select_related("request__company")
    attempted = 0
    for push in due.order_by("pk"):
        try:
            deliver(push)
        except Exception:
            logger.exception("farm-in: status push %s failed unexpectedly", push.pk)
        attempted += 1
    return attempted + _expire_pending(now)


def _expire_pending(now: datetime) -> int:
    """Expire each pending request an hour past pickup and mark its lead Lost.

    Each row is re-read under its lock, so a request accepted since the scan stays accepted.
    A lead already Lost keeps its reason; a Booked one is flagged for review instead
    (`farm_in.close_lead`). A row that fails is rolled back and logged, stays pending for
    the next run, and never blocks the rows after it. Returns how many rows were attempted.
    """
    due = FarmInRequest.objects.filter(
        status=FarmInRequest.Status.PENDING, pickup_at__lt=now - EXPIRE_AFTER
    )
    attempted = 0
    for pk in due.order_by("pk").values_list("pk", flat=True):
        try:
            if not _expire_one(pk, now):
                continue
        except Exception:
            logger.exception("farm-in: expiring request %s failed", pk)
        attempted += 1
    return attempted


def _expire_one(pk: int, now: datetime) -> bool:
    """Expire one request if it is still pending; False when someone decided it meanwhile."""
    with transaction.atomic():
        req = (
            FarmInRequest.objects.select_for_update(of=("self",))
            .select_related("lead")
            .filter(pk=pk, status=FarmInRequest.Status.PENDING)
            .first()
        )
        if req is None:
            return False
        booked = req.lead.status == req.lead.Status.BOOKED
        req.status = FarmInRequest.Status.EXPIRED
        req.alert = "" if booked else EXPIRED_ALERT  # a booked lead gets the review alert
        req.last_event_at = now
        farm_in.close_lead(
            req,
            EXPIRED_LOST_REASON,
            review_alert=EXPIRED_BOOKED_ALERT,
            title="GNet request expired on a booked order",
        )
        req.save(update_fields=["status", "alert", "last_event_at", "updated_at"])
    return True
