"""Dispatch assignment -> GNet gateway sync orchestration.

Mirrors apps.integrations.la_sync.push_reservation's shape: an idempotent GnetEvent
audit log keyed by idempotency_key, a preview mode that never touches the network,
and alert-on-failure with no automatic retry. This module only decides *whether* and
*how* a send happens and records the outcome — it does not change any existing
dispatch flow (Task 4 wires push_assignment/cancel_assignment into the board).

SAFETY: the GNet gateway is deployed in production and talks to real GNet — a
successful send books a REAL vehicle with a REAL affiliate. Preview mode is the only
thing standing between this code path and a real booking:

    preview <=> (not settings.GNET_ACTIVE) or (not settings.GNET_API_KEY)

Both flags default to off/blank (see config/settings/base.py), so a fresh
environment previews by default and must be armed deliberately. `send_trip` /
`cancel_trip` are imported as bare names (not via the `gnet` module) so tests can
patch them directly on this module with `patch.object(gnet_sync, "send_trip")` and
assert they were never called.
"""

import json

from django.conf import settings
from django.db import transaction

from apps.integrations.gnet import (
    GnetAPIError,
    GnetNotConfigured,
    build_send_payload,
    cancel_trip,
    send_trip,
)
from apps.notifications.models import Notification

from .models import Assignment, GnetEvent

SEND_PREFIX = "send_trip-a"
CANCEL_PREFIX = "cancel_trip-a"


def _is_preview() -> bool:
    """True unless GNet is both armed (`GNET_ACTIVE`) and credentialed (`GNET_API_KEY`)."""
    return not settings.GNET_ACTIVE or not settings.GNET_API_KEY


def _fail(event: GnetEvent, assignment: Assignment, message: str, *, title: str) -> GnetEvent:
    """Record an ERROR on `event` and alert a human. Never retries.

    A 409 means the original send was claimed but its outcome is ambiguous; a
    422/502/503 doesn't prove the trip wasn't created either. Resending under the
    same requesterResNo (== this assignment's pk, per build_send_payload) risks
    booking a second real vehicle with a real affiliate, so this function's only
    job is to surface the failure for a person to resolve — never to retry it.
    """
    event.result = GnetEvent.Result.ERROR
    event.response = message[:2000]
    event.save(update_fields=["payload", "result", "response", "updated_at"])
    Notification.notify(
        assignment.reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=title[:160],
        detail=f"Assignment #{assignment.pk}: {message}"[:255],
    )
    return event


def push_assignment(assignment: Assignment) -> GnetEvent:
    """Send `assignment`'s trip to the GNet gateway (or record a preview).

    Idempotent per assignment via the unique `send_trip-a<pk>` key. Ordering matters:

    1. If the event already succeeded, return it untouched — sent nothing. This runs
       *before* the preview check so a completed trip is never re-sent, not even in
       preview.
    2. Build the payload. A `GnetNotConfigured` here is a local refusal (unmapped
       vehicle, no vendor griddID, <2 stops): record ERROR, alert, and stop — the
       gateway is never called.
    3. In preview, store the full payload we would have sent, mark PREVIEW, and stop.
    4. Otherwise send. A `deduped: true` response is a success (the original send
       already landed); any other non-2xx is an ERROR that is alerted and never
       retried by this function.
    """
    event, _ = GnetEvent.objects.get_or_create(
        assignment=assignment,
        action=GnetEvent.Action.SEND_TRIP,
        idempotency_key=f"{SEND_PREFIX}{assignment.pk}",
    )
    if event.result == GnetEvent.Result.SUCCESS:
        return event

    try:
        payload = build_send_payload(assignment)
    except GnetNotConfigured as exc:
        return _fail(event, assignment, str(exc), title="GNet send failed")

    event.payload = payload
    if _is_preview():
        event.result = GnetEvent.Result.PREVIEW
        event.response = "Preview — nothing sent to GNet."
        event.save(update_fields=["payload", "result", "response", "updated_at"])
        return event

    try:
        data = send_trip(payload)
    except GnetAPIError as exc:
        return _fail(event, assignment, f"{exc.status}: {exc.body}", title="GNet send failed")

    # `deduped: true` means this requesterResNo already succeeded once — the gateway
    # still returns the original transactionId (see gnet.send_trip's docstring), so
    # this is treated exactly like a fresh success. Fall back to any id already on
    # the assignment only if the response is missing one outright.
    transaction_id = data.get("transactionId") or assignment.gnet_transaction_id
    with transaction.atomic():
        # Losing the transaction id here makes the trip permanently uncancellable —
        # the gateway has no lookup-by-requesterResNo endpoint — so this save and the
        # event's SUCCESS update happen inside the same transaction.
        assignment.gnet_transaction_id = transaction_id
        assignment.save(update_fields=["gnet_transaction_id", "updated_at"])
        event.result = GnetEvent.Result.SUCCESS
        event.response = json.dumps(data)
        event.save(update_fields=["payload", "result", "response", "updated_at"])
    return event


def cancel_assignment(assignment: Assignment) -> GnetEvent | None:
    """Cancel `assignment`'s GNet trip, or no-op if it was never sent.

    Returns None when there's no `gnet_transaction_id` — nothing was ever booked on
    the gateway, so there's nothing to cancel. Otherwise mirrors push_assignment's
    idempotent-event / preview / alert-on-failure shape on the `cancel_trip-a<pk>` key.
    """
    if not assignment.gnet_transaction_id:
        return None

    event, _ = GnetEvent.objects.get_or_create(
        assignment=assignment,
        action=GnetEvent.Action.CANCEL_TRIP,
        idempotency_key=f"{CANCEL_PREFIX}{assignment.pk}",
    )
    if event.result == GnetEvent.Result.SUCCESS:
        return event

    event.payload = {"transactionId": assignment.gnet_transaction_id}
    if _is_preview():
        event.result = GnetEvent.Result.PREVIEW
        event.response = "Preview — nothing sent to GNet."
        event.save(update_fields=["payload", "result", "response", "updated_at"])
        return event

    try:
        data = cancel_trip(assignment.gnet_transaction_id)
    except GnetAPIError as exc:
        return _fail(event, assignment, f"{exc.status}: {exc.body}", title="GNet cancel failed")

    event.result = GnetEvent.Result.SUCCESS
    event.response = json.dumps(data)
    event.save(update_fields=["payload", "result", "response", "updated_at"])
    return event
