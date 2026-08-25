"""Interpret GNet farm-out callbacks (GNET-CONNECTION-GUIDE.md §5.8) and resolve the
assignment they concern.

This module decides *what a status means* for an `Assignment`; the HTTP boundary
(signature check, JSON parsing) lives in `apps.integrations.views.gnet_callback`,
same split as the outbound side (`apps.integrations.gnet` is the HTTP client,
`apps.dispatch.gnet_sync` is the orchestration). Every state change goes through
`apps.dispatch.services` — this module never mutates `Assignment` directly.

The gateway deliberately does not enforce its own status allowlist on the way in:
an unrecognised or purely informational partner status (`EN_ROUTE`, `NO_SHOW`, ...)
is still a real update and must be recorded, never dropped or allowed to crash the
receiver.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from apps.notifications.models import Notification

from . import services
from .models import Assignment, GnetEvent
from .services import AssignmentError

logger = logging.getLogger(__name__)

# Callback statuses that mean "the affiliate accepted" (task brief's status-mapping
# table / contract v2 §5.11's worked example uses ASSIGNED for exactly this). REJECT,
# CANCEL, FAILED, and CLOSE are handled explicitly in `_apply_status`; anything else
# — ON_LOCATION, EN_ROUTE, NO_SHOW, PASSENGER_ON_BOARD, or a string GNet invents
# tomorrow — is recorded but changes no state.
_CONFIRM_STATUSES = frozenset({"CONFIRMED", "ACKNOWLEDGED", "ASSIGNED"})


def _parse_amount(raw: object) -> Decimal | None:
    """`totalAmount` is a string in this contract (§5.10); anything else — a JSON
    number, `None`, an unparseable string — means "absent," not an error. Parsing
    must never raise: a malformed amount from a partner is not our bug to crash on.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _notes_text(affiliate_reservation: object) -> str:
    """`affiliateReservation.notes` is sometimes a bare string, more often a list of
    `{message, context}` objects (§5.9) — the only explanation we get for why a
    partner declined. Normalize either shape to one string; anything else (missing,
    null, an unexpected type) becomes "".
    """
    if not isinstance(affiliate_reservation, dict):
        return ""
    notes = affiliate_reservation.get("notes")
    if isinstance(notes, str):
        return notes
    if isinstance(notes, list):
        messages = [
            str(item["message"]) for item in notes if isinstance(item, dict) and item.get("message")
        ]
        return "; ".join(messages)
    return ""


def _alert(assignment: Assignment, *, title: str, detail: str) -> None:
    """Anchor on the reservation's lead — `Notification.lead` is non-nullable."""
    Notification.notify(
        assignment.reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=title[:160],
        detail=detail[:255],
    )


def _apply_amount(assignment: Assignment, payload: dict, status: str) -> None:
    """Auto-heal `assignment.payout` — but only when an amount means something.

    On GNet the affiliate prices the trip, not APC (unlike the manual trip-sheet
    email flow), so a callback that reports real coverage supersedes whatever
    payout is already on file: "when they price it and we get the change it auto
    heals." But `REJECT` and `FAILED` mean nobody is covering the trip — no amount
    in either payload is a payout we owe, and `FAILED` in particular leaves the
    assignment OFFERED, so writing a price there would put one on a still-open
    offer. Both are skipped entirely, before `totalAmount` is even parsed.

    `CANCEL` still auto-heals: it happens after acceptance, so an amount there is a
    plausible cancellation charge, and `CANCEL` already withdraws the assignment
    and alerts, so a human sees it either way.

    Alerts only for the one pricing event a broker needs told about: a `CLOSE`
    amount that differs from what was already recorded. A first quote landing on
    top of a dispatcher's manual estimate — or any other non-CLOSE update — is
    silent.
    """
    if status in {"REJECT", "FAILED"}:
        return
    amount = _parse_amount(payload.get("totalAmount"))
    if amount is None:
        return
    previous = assignment.payout
    if amount == previous:
        return
    if status == "CLOSE":
        _alert(
            assignment,
            title=f"GNet closed assignment #{assignment.pk} at a different price",
            detail=(
                f"Trip #{assignment.reservation_id}: quoted {previous}, "
                f"affiliate closed at {amount}."
            ),
        )
    assignment.payout = amount
    assignment.save(update_fields=["payout", "updated_at"])


def _apply_status(assignment: Assignment, status: str, payload: dict) -> None:
    """Resolve `assignment` per the callback's status. Every transition is a call
    into `dispatch.services` — never a direct `Assignment` mutation.

    An `AssignmentError` (an illegal transition — the assignment was already
    resolved by an earlier callback or a dispatcher action) is caught and logged,
    not raised: the caller already records this callback as a `GnetEvent`, and the
    gateway retries a non-2xx up to 3 times, so surfacing this as a failure would
    only cause a retry storm that helps nobody.
    """
    affiliate_reservation = payload.get("affiliateReservation")
    try:
        if status in _CONFIRM_STATUSES:
            services.confirm(assignment)
        elif status == "REJECT":
            services.decline(assignment, note=_notes_text(affiliate_reservation))
        elif status == "CANCEL":
            services.withdraw(assignment, note="Cancelled by the affiliate via GNet.")
            _alert(
                assignment,
                title=f"GNet cancelled assignment #{assignment.pk}",
                detail=f"Trip #{assignment.reservation_id} is uncovered again.",
            )
        elif status == "FAILED":
            reason = _notes_text(affiliate_reservation) or "No reason given by the affiliate."
            _alert(
                assignment,
                title=f"GNet offer failed for assignment #{assignment.pk}",
                detail=f"Trip #{assignment.reservation_id}: {reason}",
            )
        elif status == "CLOSE":
            pass  # no state transition here — final pricing is _apply_amount's job
        else:
            logger.info(
                "GNet callback: unrecognised status %r for assignment #%s — recorded only.",
                status,
                assignment.pk,
            )
    except AssignmentError as exc:
        logger.info(
            "GNet callback: status %r ignored for assignment #%s (%s).",
            status,
            assignment.pk,
            exc,
        )


def handle_callback(payload: dict) -> GnetEvent:
    """Apply one GNet farm-out callback and record it, or no-op on a repeat.

    Never raises. `transaction_id` must be read from the BODY, not
    `X-Lansdowne-Transaction-Id` (which can be an empty string when the gateway
    correlated via `requesterResNo` instead — see the view). A `transactionId` that
    doesn't match any assignment still gets a `GnetEvent` recorded (uncorrelated,
    `assignment=None`) rather than being dropped — it's evidence a callback fired
    for a trip we can't currently place.

    Dedupe is on `transaction_id` + `status` (`f"callback-{transaction_id}-
    {status}"`), per the contract's instruction to dedupe independently of the
    gateway's own payload-based dedupe. `GnetEvent.idempotency_key`'s unique
    constraint makes `get_or_create` the atomic guard: a repeat delivery finds the
    existing row and returns without reapplying anything.
    """
    transaction_id = str(payload.get("transactionId") or "")
    status = str(payload.get("status") or "")
    idempotency_key = f"callback-{transaction_id}-{status}"

    assignment = None
    if transaction_id:
        assignment = Assignment.objects.filter(gnet_transaction_id=transaction_id).first()

    event, created = GnetEvent.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "assignment": assignment,
            "action": GnetEvent.Action.CALLBACK,
            "payload": payload,
            "result": GnetEvent.Result.SUCCESS,
        },
    )
    if not created:
        return event

    if assignment is not None:
        _apply_amount(assignment, payload, status)
        _apply_status(assignment, status, payload)

    return event
