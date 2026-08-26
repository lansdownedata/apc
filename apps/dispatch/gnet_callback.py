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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from apps.integrations.gnet import RESNO_PREFIX
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

# Same ceiling `dispatch.views._payout` enforces, and for the same two reasons: a value
# at or above it overflows MoneyField(max_digits=10, decimal_places=2) once rounded, and
# quantize() itself raises InvalidOperation on a number large enough to blow the 28-digit
# context precision. Judged BEFORE rounding — don't "simplify" this to 1e8.
_PAYOUT_CEILING = Decimal("99999999.995")

# Bounds for the parts that compose `idempotency_key` (CharField(max_length=160)). A
# partner id or status longer than the column used to raise DataError from the inbound
# hot path — with finding 2's atomic() that is now merely a retried 500 rather than a
# lost callback, but a key that cannot be written is still a callback that can never be
# applied. 9 ("callback-") + 100 + 1 + 40 = 150.
_MAX_KEY_CORRELATOR = 100
_MAX_KEY_STATUS = 40

# `Assignment.pk` is a BigAutoField; anything outside the signed 64-bit range is not a
# pk we could ever hold, and handing it to the ORM risks a database-level range error
# on the public callback endpoint.
_MAX_PK = 2**63 - 1


def _parse_amount(raw: object) -> Decimal | None:
    """`totalAmount` as money we could actually store, or None.

    `totalAmount` is a string in this contract (§5.10); anything else — a JSON number,
    `None`, an unparseable string — means "absent," not an error. Parsing must never
    raise: a malformed amount from a partner is not our bug to crash on.

    Out-of-range is "absent" too, deliberately rather than an exception. This writes the
    very same `Assignment.payout` that `dispatch.views._payout` guards, so it enforces
    the same three rules — finite, non-negative, below the ceiling — and quantizes to
    cents for the same reason (MySQL rounds a third decimal half-even, Postgres half-up).
    Without them a `CLOSE` carrying `"-500.00"` was accepted and silently inflated the
    trip's margin, and `"1e999"`/`"NaN"` reached the database as a `DataError` /
    `InvalidOperation` — a 500 on an unauthenticated-by-payload endpoint.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    # is_finite() first: NaN/Infinity parse fine, and comparing a NaN raises.
    if not value.is_finite() or value < 0 or value >= _PAYOUT_CEILING:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _resno_pk(affiliate_reservation: object) -> int | None:
    """The `Assignment` pk behind `affiliateReservation.requesterResNo`, or None.

    We send `apc-<pk>` (`apps.integrations.gnet.build_send_payload`), so the prefix is
    required and comes back off here — the two must change together. Anything that isn't
    a plausible namespaced pk (a bare pk, text, the bare prefix, an out-of-range integer,
    a nested object) is None: an uncorrelatable callback is still recorded as evidence,
    never a crash.
    """
    if not isinstance(affiliate_reservation, dict):
        return None
    raw = affiliate_reservation.get("requesterResNo")
    if raw is None or isinstance(raw, (dict, list, bool)):
        return None
    text = str(raw).strip()
    # REQUIRED, not merely stripped if present: a `removeprefix` makes the namespace
    # optional, which enforces it only on the gateway's side of the wire and leaves a
    # bare pk — every assignment has one — able to correlate here.
    if not text.startswith(RESNO_PREFIX):
        return None
    try:
        value = int(text[len(RESNO_PREFIX) :])
    except ValueError:
        return None
    return value if 0 < value <= _MAX_PK else None


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

    A pre-`CLOSE` heal alerts only when it moves a payout that was ALREADY quoted. On
    this send-now-price-later channel the affiliate's first quote lands in an empty
    payout (`MoneyField` defaults to 0 and a GNet offer is created at 0.00), so alerting
    on any move would fire on every single farm-out — a `SYNC_FAILED` notification, the
    same kind that carries "GNet send failed", on the happy path. That is how real
    alerts get ignored, and a first quote arriving into an empty payout is normal
    (client decision, 2026-08-25). A SECOND, changed quote is the exception worth
    raising: it silently moves a margin the dispatcher has already seen.

    The `CLOSE` mismatch alert stays unconditional — a final price is the one pricing
    event a broker reconciles against, empty prior quote or not.

    An amount equal to what is already recorded changes nothing and says nothing, so a
    run of statuses repeating one price alerts at most once.
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
    elif previous > 0:
        _alert(
            assignment,
            title=f"GNet repriced assignment #{assignment.pk}",
            detail=(
                f"Trip #{assignment.reservation_id}: payout {previous} -> {amount} "
                f"on {status or 'an unnamed status'} — margin has changed."
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
            # `release_gateway=False`: the affiliate is the one cancelling, so the trip
            # is already released on their side. A DELETE back would be rejected and
            # would raise a spurious "GNet cancel failed" alert next to the correct one
            # below — on every affiliate cancellation — while spending a 10s-timeout
            # outbound call inside the gateway's 15s callback budget.
            services.withdraw(
                assignment,
                note="Cancelled by the affiliate via GNet.",
                release_gateway=False,
            )
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


def handle_callback(payload: object) -> GnetEvent:
    """Apply one GNet farm-out callback and record it, or no-op on a repeat.

    Never raises — including when `payload` isn't a dict. The view is expected to
    reject a non-object JSON body before calling this (a syntactically valid
    `[1,2,3]`, `"str"`, a bare number/bool/`null` all parse fine but have no
    `.get`), but this function doesn't trust that alone: a non-dict `payload` is
    treated the same as `{}` rather than crashing.

    `transaction_id`/`status` are read from the BODY, not
    `X-Lansdowne-Transaction-Id` (which can be an empty string when the gateway
    correlated via `requesterResNo` instead — see the view). Each is checked
    against `is None` rather than a bare `or ""` — a present-but-falsy value (`0`,
    `False`) must still count as present, not be silently treated as absent.

    A `transactionId` that doesn't match any assignment still gets a `GnetEvent`
    recorded (uncorrelated, `assignment=None`) rather than being dropped — it's
    evidence a callback fired for a trip we can't currently place.

    When the body carries NO `transactionId`, correlation falls back to
    `affiliateReservation.requesterResNo` — which is this assignment's pk, namespaced
    (see `_resno_pk`). The gateway's own `callbackSchema` permits that body and its
    `correlate()` implements the same fallback, so a callback shaped that way is
    ordinary traffic, not an anomaly. The id still wins whenever both are present: it is
    the gateway's own correlator.

    Dedupe is on the correlator + status (`f"callback-{correlator}-{status}"`), per the
    contract's instruction to dedupe independently of the gateway's own payload-based
    dedupe. The resNo goes into the key when there is no transaction id — otherwise
    every id-less callback would collide on one `callback--<STATUS>` key and the second
    one, for a completely different assignment, would be discarded as a repeat.
    `GnetEvent.idempotency_key`'s unique constraint makes `get_or_create` the atomic
    guard: a repeat delivery finds the existing row and returns without reapplying.

    The whole body runs in one `transaction.atomic()`. There is no `ATOMIC_REQUESTS`, so
    without it the dedupe row committed on its own and any later failure was permanent:
    the view 500'd, and every one of the gateway's retries then found that row, returned
    200, and never applied the status — the assignment stuck OFFERED while the affiliate
    was confirmed. Rolling back together means a retry can genuinely re-apply.
    """
    if not isinstance(payload, dict):
        payload = {}
    raw_transaction_id = payload.get("transactionId")
    transaction_id = "" if raw_transaction_id is None else str(raw_transaction_id)
    raw_status = payload.get("status")
    status = "" if raw_status is None else str(raw_status)

    assignment = None
    correlator = ""
    if transaction_id:
        correlator = transaction_id
        assignment = Assignment.objects.filter(gnet_transaction_id=transaction_id).first()
    elif (res_pk := _resno_pk(payload.get("affiliateReservation"))) is not None:
        correlator = f"resno:{res_pk}"
        # Channel-filtered: a pk matches ANY assignment, including one arranged by phone
        # that never went near the gateway. The transactionId path can't reach those (a
        # manual assignment's id is blank, which short-circuits above), so this keeps the
        # fallback's reach identical to the correlator it stands in for.
        assignment = Assignment.objects.filter(pk=res_pk, channel=Assignment.Channel.GNET).first()

    idempotency_key = f"callback-{correlator[:_MAX_KEY_CORRELATOR]}-{status[:_MAX_KEY_STATUS]}"

    with transaction.atomic():
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
