"""Queue that delivers farm-in status pushes to the GNet gateway, with retries.

The gateway never retries anything on its side, so every `FarmInStatusPush` is retried
here: transport failures, 408/409/429 and 5xx back off exponentially (capped at 30 minutes)
up to `MAX_ATTEMPTS`; any other refusal fails at once and raises an alert for the desk.

A status is pushed once. A `sent` push blocks a repeat of the same status, except for the
`REPEATABLE` ones (a trip can be reassigned or re-canceled), and a `pending` push for a
status always blocks another.
"""

import logging
import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.integrations import gnet
from apps.integrations.gnet import GnetAPIError
from apps.notifications.models import Notification

logger = logging.getLogger(__name__)

REPEATABLE = frozenset({"ASSIGNED", "CANCEL"})
MAX_ATTEMPTS = 8
RETRY_STATUSES = frozenset({0, 408, 409, 429})
EXPIRE_AFTER = timedelta(hours=1)
_MAX_BACKOFF = timedelta(minutes=30)
LEASE = timedelta(seconds=60)
_ALERT_PREFIX = "GNet "


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
    """Queue a status push and try it once the transaction commits; None when blocked."""
    if _blocker(req, status):
        return None
    return _create(req, status, total_amount)


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


def _fail(push: FarmInStatusPush, reason: str) -> FarmInStatusPush:
    """Mark a push terminally failed and tell the desk."""
    push.state = FarmInStatusPush.State.FAILED
    push.last_error = reason[:500]
    push.save(update_fields=["state", "last_error", "attempts", "updated_at"])
    req = push.request
    req.alert = f"{_ALERT_PREFIX}{push.status} not delivered: {reason}"[:255]
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
    if req.alert.startswith(f"{_ALERT_PREFIX}{push.status} not delivered"):
        req.alert = ""
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

    Expiry sends nothing to GNet (the trip simply passed) and raises no notification.
    Returns pushes attempted plus requests expired.
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
    expired = FarmInRequest.objects.filter(
        status=FarmInRequest.Status.PENDING, pickup_at__lt=now - EXPIRE_AFTER
    ).update(
        status=FarmInRequest.Status.EXPIRED,
        alert="Expired: pickup time passed",
        last_event_at=now,
    )
    return attempted + expired
