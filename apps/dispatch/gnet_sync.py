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


def _fail(
    event: GnetEvent,
    assignment: Assignment,
    message: str,
    *,
    title: str,
    payload: dict | None = None,
    subject: str = "",
) -> GnetEvent:
    """Record an ERROR on `event` and alert a human. Never retries.

    A 409 means the original send was claimed but its outcome is ambiguous; a
    422/502/503 doesn't prove the trip wasn't created either. Resending under the
    same requesterResNo (== this assignment's pk, per build_send_payload) risks
    booking a second real vehicle with a real affiliate, so this function's only
    job is to surface the failure for a person to resolve — never to retry it.

    `payload` is only passed (and only then written to `update_fields`) when the
    caller actually built one before failing — a `GnetNotConfigured` refusal never
    reaches that point, so its event's `payload` field is left exactly as it was.

    `subject` is extra identity for the notification only — never for `event.response`,
    which stays the verbatim gateway message. It goes BEFORE the message so the 255-char
    truncation can't eat it: on the cancel path it carries the transaction id, and the
    alert may end up being the only place that id survives (see `cancel_assignment`).
    """
    event.result = GnetEvent.Result.ERROR
    event.response = message[:2000]
    update_fields = ["result", "response", "updated_at"]
    if payload is not None:
        event.payload = payload
        update_fields.append("payload")
    event.save(update_fields=update_fields)
    Notification.notify(
        assignment.reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=title[:160],
        detail=f"Assignment #{assignment.pk}{subject}: {message}"[:255],
    )
    return event


def _preview(event: GnetEvent, assignment: Assignment, *, what: str) -> GnetEvent:
    """Mark `event` PREVIEW — keeping the exact payload the caller staged on it — and
    tell a human that nothing was sent.

    Preview is otherwise invisible from the portal: the assignment is created OFFERED
    either way, so a previewed offer looks farmed out while nothing left the building,
    discoverable only in Django admin. That bites the moment someone types a griddID
    onto a vendor record — a normal admin action, no deploy — and that affiliate's
    offers start vanishing. The email channel already alerts when a send fails; this is
    the same courtesy for a send that never happened.

    Deliberately an alert and NOT a retry: arming GNET_ACTIVE later must not blast real
    bookings for offers that have gone stale in the meantime.
    """
    event.result = GnetEvent.Result.PREVIEW
    event.response = "Preview — nothing sent to GNet."
    event.save(update_fields=["payload", "result", "response", "updated_at"])
    Notification.notify(
        assignment.reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=f"{what} not sent — GNet is in preview mode"[:160],
        detail=(
            f"Assignment #{assignment.pk} for trip #{assignment.reservation_id} was "
            "recorded but never reached the network. Arrange coverage directly."
        )[:255],
    )
    return event


def push_assignment(assignment: Assignment) -> GnetEvent:
    """Send `assignment`'s trip to the GNet gateway (or record a preview).

    Idempotent per assignment via the unique `send_trip-a<pk>` key. Ordering matters:

    1. If the event already resolved — SUCCESS *or* ERROR — return it untouched,
       sending nothing. The SUCCESS half runs before the preview check so a completed
       trip is never re-sent, not even in preview. The ERROR half exists because a
       failed send must never be retried under the same requesterResNo (== this
       assignment's pk): `Assignment` is an append-only model precisely so that
       retrying a farm-out means creating a *new* Assignment row, which gets a new pk
       and therefore a genuinely new, safe requesterResNo — not calling this function
       again on the one that already failed.
    2. Build the payload. A `GnetNotConfigured` here is a local refusal (unmapped
       vehicle, no vendor griddID, <2 stops): record ERROR, alert, and stop — the
       gateway is never called.
    3. In preview, store the full payload we would have sent, mark PREVIEW, and stop.
    4. Otherwise send. A 2xx with no usable `transactionId` is treated as an ERROR,
       not a success (see below) — any other non-2xx is likewise an ERROR that is
       alerted and never retried by this function.
    """
    event, _ = GnetEvent.objects.get_or_create(
        assignment=assignment,
        action=GnetEvent.Action.SEND_TRIP,
        idempotency_key=f"{SEND_PREFIX}{assignment.pk}",
    )
    if event.result in (GnetEvent.Result.SUCCESS, GnetEvent.Result.ERROR):
        return event

    try:
        payload = build_send_payload(assignment)
    except GnetNotConfigured as exc:
        return _fail(event, assignment, str(exc), title="GNet send failed")

    event.payload = payload
    if _is_preview():
        return _preview(event, assignment, what="Offer")

    try:
        data = send_trip(payload)
    except GnetAPIError as exc:
        return _fail(
            event,
            assignment,
            f"{exc.status}: {exc.body}",
            title="GNet send failed",
            payload=payload,
        )

    # `deduped: true` means this requesterResNo already succeeded once — the gateway
    # still returns the original transactionId on a normal dedup response (see
    # gnet.send_trip's docstring), so that case is handled by the plain `or` below.
    # The only reason to fall back to whatever's already on the assignment is a
    # deduped response that, unusually, omits transactionId outright.
    #
    # `.strip()` matters: a whitespace-only string is truthy in Python, so an
    # unstripped `"   "` would sail past every check below and commit as a terminal
    # SUCCESS with a garbage id. The relay applies this exact same trim for the
    # exact same reason (parseSendTripResult in the gateway repo).
    transaction_id = (data.get("transactionId") or "").strip()
    if not transaction_id and data.get("deduped") and assignment.gnet_transaction_id:
        transaction_id = assignment.gnet_transaction_id

    if not transaction_id:
        # A 2xx with no usable id must NOT become a terminal SUCCESS: SUCCESS
        # short-circuits every future push (see point 1 above) and cancel_assignment
        # no-ops on a blank id, so a false success here would leave the trip
        # permanently unretryable AND uncancellable — with a real vehicle possibly
        # already dispatched and nobody told. Treat it as an ERROR and let a human
        # reconcile it in the gateway before farming this trip out again (as a new
        # Assignment — see point 1).
        return _fail(
            event,
            assignment,
            "Gateway returned a 2xx with no usable transactionId "
            f"(response={json.dumps(data)[:1900]}) — a trip may already exist on "
            "the gateway; reconcile manually in GNet before farming this trip out "
            "again.",
            title="GNet send failed",
            payload=payload,
        )

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

    cancel_payload = {"transactionId": assignment.gnet_transaction_id}
    event.payload = cancel_payload
    if _is_preview():
        # Only reachable when a live send preceded a flip of GNET_ACTIVE back off — and
        # that is exactly the case worth shouting about: a real affiliate is still
        # holding a trip that this cancel did not release.
        return _preview(event, assignment, what="Cancel")

    try:
        data = cancel_trip(assignment.gnet_transaction_id)
    except GnetAPIError as exc:
        return _fail(
            event,
            assignment,
            f"{exc.status}: {exc.body}",
            title="GNet cancel failed",
            payload=cancel_payload,
            # The alert has to be self-sufficient. `reservation_delete` calls
            # `release_trips` and then deletes the reservation, which CASCADEs the
            # Assignment and every GnetEvent with it — so when a cancel has just failed
            # (a 502/503, exactly when failures cluster) this notification can be the
            # only surviving copy of the id needed to reconcile the booking in GNet.
            subject=f" (GNet transaction {assignment.gnet_transaction_id})",
        )

    event.result = GnetEvent.Result.SUCCESS
    event.response = json.dumps(data)
    event.save(update_fields=["payload", "result", "response", "updated_at"])
    return event
