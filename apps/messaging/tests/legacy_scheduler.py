"""The old scheduler, frozen as it stood before every caller moved to `messaging.events`
(APC-118, Task 11). Test-only: the parity tests in `test_events.py` run it beside `events`
on the same order and compare what each queues. Nothing in the app imports it; its sending
half is gone (`messaging.sender` replaced it).
"""

from datetime import datetime, time

from django.utils import timezone

from apps.messaging import overrides
from apps.messaging.models import TouchPoint
from apps.messaging.rules import DRIVER_MESSAGE_STATUSES

_QUOTE_KINDS = (
    TouchPoint.Kind.TP3_QUOTE_SENT_SMS,
    TouchPoint.Kind.TP4_VIEWED_SMS,
    TouchPoint.Kind.TP5_VIEWED_EMAIL,
    TouchPoint.Kind.TP6_QUOTE_FOLLOWUP,
    TouchPoint.Kind.TP7_EXPIRING,
    TouchPoint.Kind.TP8_EXPIRED,
)

_K = TouchPoint.Kind
# Reservation-anchored kinds (APC-18-22): they carry a `reservation` FK and fire for a
# BOOKED lead — the inverse of the TP1-8 / quote skip rules.
_SERVICE_DATE_KINDS = frozenset(
    {
        _K.WED_FINAL_DETAILS,
        _K.TRIP_CONFIRM_CUSTOMER,
        _K.TRIP_CONFIRM_CUSTOMER_2,
        _K.TRIP_CONFIRM_AFFILIATE,
    }
)
# The two customer waves are one message sent twice: each collapses to a single send
# per customer per pickup date (APC-19).
_CUSTOMER_CONFIRM_KINDS = frozenset({_K.TRIP_CONFIRM_CUSTOMER, _K.TRIP_CONFIRM_CUSTOMER_2})
_TRIGGERED_KINDS = frozenset(
    {
        _K.DRIVER_RELEASED,
        _K.DRIVER_UPDATED,
        _K.STATUS_DISPATCHED,
        _K.STATUS_ON_THE_WAY,
        _K.STATUS_ARRIVED,
    }
)
_DRIVER_KINDS = frozenset({_K.DRIVER_RELEASED, _K.DRIVER_UPDATED})
_RESERVATION_KINDS = _SERVICE_DATE_KINDS | _TRIGGERED_KINDS

# Lead-level outcome messages (APC-26): how the customer's wait ended. Each fires on a
# lead status the quote-program skips exist to stop — that's why they get their own branch.
_ORDER_KINDS = frozenset({_K.ORDER_AUTH_EXPIRED, _K.ORDER_CONFIRMED, _K.ORDER_CANCELLED})

# Trip status -> the customer notification kind it fires (APC-22).
_STATUS_KINDS = {
    "on_the_way": _K.STATUS_ON_THE_WAY,
    "arrived": _K.STATUS_ARRIVED,
}


def _create(lead, kind: str, anchor_dt) -> TouchPoint | None:
    """Create a lead-level row, unless the owner has switched that message off."""
    if not overrides.is_enabled(kind):
        return None
    return TouchPoint.objects.create(
        lead=lead, kind=kind, scheduled_for=anchor_dt + overrides.effective(kind).offset
    )


def schedule_lead_created(lead) -> None:
    """Schedule the welcome (TP1) and lead-follow-up (TP2) touch-points for a new lead."""
    now = timezone.now()
    _create(lead, TouchPoint.Kind.TP1_WELCOME, now)
    _create(lead, TouchPoint.Kind.TP2_LEAD_FOLLOWUP, now)


def schedule_quote_sent(lead) -> None:
    """(Re)build the post-quote program from the lead's sent/expiry stamps."""
    cancel_pending(lead, kinds=_QUOTE_KINDS)
    sent = lead.quote_sent_at or timezone.now()
    _create(lead, TouchPoint.Kind.TP3_QUOTE_SENT_SMS, sent)
    _create(lead, TouchPoint.Kind.TP6_QUOTE_FOLLOWUP, sent)
    expires = lead.quote_expires_at
    if expires:
        if expires + overrides.effective(TouchPoint.Kind.TP7_EXPIRING).offset > timezone.now():
            _create(lead, TouchPoint.Kind.TP7_EXPIRING, expires)
        _create(lead, TouchPoint.Kind.TP8_EXPIRED, expires)


def schedule_quote_viewed(lead) -> None:
    """Schedule the quote-viewed nudges (TP4/TP5) once per lead.

    CANCELLED rows (e.g. superseded by a quote re-send) don't count as "already
    scheduled" — otherwise a re-sent quote could never get its viewed nudges again.
    SENT/SCHEDULED/SKIPPED/FAILED still block: a customer who already got the nudge
    shouldn't be re-nudged.
    """
    viewed_kinds = (TouchPoint.Kind.TP4_VIEWED_SMS, TouchPoint.Kind.TP5_VIEWED_EMAIL)
    if (
        TouchPoint.objects.filter(lead=lead, kind__in=viewed_kinds)
        .exclude(status=TouchPoint.Status.CANCELLED)
        .exists()
    ):
        return
    now = timezone.now()
    for kind in viewed_kinds:
        _create(lead, kind, now)


def schedule_review_request(lead) -> None:
    """Schedule the post-trip review request, idempotently."""
    kind = TouchPoint.Kind.REVIEW_REQUEST
    if TouchPoint.objects.filter(lead=lead, kind=kind).exists():
        return
    _create(lead, kind, timezone.now())


def schedule_post_trip(lead) -> None:
    """The order has finished: the thank-you (APC-62), then the review request a day
    later. Idempotent, one row of each per lead. Called from both places that learn an
    order is done — the LimoAnywhere webhook and the task engine's post-trip entry."""
    kinds = (TouchPoint.Kind.THANK_YOU, TouchPoint.Kind.REVIEW_REQUEST)
    have = set(TouchPoint.objects.filter(lead=lead, kind__in=kinds).values_list("kind", flat=True))
    now = timezone.now()
    for kind in kinds:
        if kind not in have:
            _create(lead, kind, now)


def schedule_payment_reminder(lead) -> None:
    """The 72-hour pre-charge notice (spec 2026-08-30 §9). One row per lead.

    A pre-charge notice, not dunning: it only makes sense for a booked lead with a card on
    file whose balance the cron will auto-charge, so it is a no-op without a card or a dated
    trip. Called from `sync_plan_from_collected` when the balance flips to SCHEDULED. The
    anchor is midnight on `balance_due_date` in the project timezone — `Reservation` has no
    per-trip zone yet (see the trip-timezone spec); revisit together with that work.
    """
    kind = TouchPoint.Kind.PAYMENT_REMINDER
    if lead.on_account:  # invoiced, never auto-charged (APC-105)
        return
    if TouchPoint.objects.filter(lead=lead, kind=kind).exists():
        return
    plan = getattr(lead, "payment", None)
    if plan is None or not plan.stripe_payment_method_id or plan.balance_due_date is None:
        return
    anchor = timezone.make_aware(
        datetime.combine(plan.balance_due_date, time.min), timezone.get_default_timezone()
    )
    _create(lead, kind, anchor)


# --- reservation-anchored scheduling (APC-18-22) --------------------------------------


def _ensure(
    reservation, kind: str, anchor_dt, *, at: datetime | None = None, rows: dict | None = None
) -> TouchPoint | None:
    """Create one reservation-anchored row unless a non-CANCELLED one already exists.

    A SENT / SKIPPED / FAILED row still blocks — the customer already got it, or we already
    decided not to send. Only a CANCELLED row (a supersede) allows a fresh one.

    ``at`` is an exact ``scheduled_for`` that overrides anchor + offset.
    """
    blocking = (
        TouchPoint.objects.filter(reservation=reservation, kind=kind)
        .exclude(status=TouchPoint.Status.CANCELLED)
        .exists()
    )
    if blocking or not overrides.is_enabled(kind, rows):
        return None
    return TouchPoint.objects.create(
        lead=reservation.lead,
        reservation=reservation,
        kind=kind,
        scheduled_for=at or anchor_dt + overrides.effective(kind, rows).offset,
    )


def _is_wedding(reservation) -> bool:
    from apps.reservations.services import is_wedding_trip

    return is_wedding_trip(reservation)


def schedule_service_touchpoints(lead) -> None:
    """(Re)build the per-trip service-date program for a booked lead. Idempotent.

    Called on booking and on a reservation save for an already-booked lead. Past anchors
    are scheduled anyway (a late booking still deserves a confirmation) — `run_touchpoints`
    picks them up on the next tick.
    """
    for res in lead.reservations.all():
        if res.pickup_at is None:
            continue
        _ensure(res, TouchPoint.Kind.TRIP_CONFIRM_CUSTOMER, res.pickup_at)
        _ensure(res, TouchPoint.Kind.TRIP_CONFIRM_CUSTOMER_2, res.pickup_at)
        if _is_wedding(res):
            _ensure(res, TouchPoint.Kind.WED_FINAL_DETAILS, res.pickup_at)


def reschedule_service_touchpoints(reservation) -> None:
    """Pickup date/time changed — cancel this trip's service-date rows (SENT ones too, so
    a trip already confirmed for the old date gets a fresh confirmation for the new one —
    `_ensure` treats any non-CANCELLED row as "already handled") and rebuild. Any existing
    acknowledgement is now for a date that no longer holds, so it's cleared along with it.
    """
    TouchPoint.objects.filter(
        reservation=reservation,
        kind__in=_SERVICE_DATE_KINDS,
        status__in=(TouchPoint.Status.SCHEDULED, TouchPoint.Status.SENT),
    ).update(status=TouchPoint.Status.CANCELLED)

    fields = []
    if reservation.customer_confirmed_at is not None:
        reservation.customer_confirmed_at = None
        fields.append("customer_confirmed_at")

    confirmed = _confirmed_assignment(reservation)
    if confirmed is not None and confirmed.affiliate_confirmed_at is not None:
        confirmed.affiliate_confirmed_at = None
        confirmed.save(update_fields=["affiliate_confirmed_at", "updated_at"])

    if fields:
        reservation.save(update_fields=[*fields, "updated_at"])

    schedule_service_touchpoints(reservation.lead)
    if confirmed is not None and reservation.pickup_at is not None:
        _ensure(reservation, TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE, reservation.pickup_at)


def schedule_affiliate_confirmation(assignment) -> None:
    """T-48h affiliate confirmation, scheduled when an affiliate confirms coverage (APC-20).

    Manual-channel only. A GNet vendor's acceptance *is* its acknowledgement — the offer
    and its answer already travel the gateway, with no email/ack-link flow to fire (GNet
    affiliates often have no email on file at all) and nothing that would ever stamp
    `affiliate_confirmed_at` for one, which is exactly what `monitoring.evaluate`'s
    AFFILIATE_UNACKED check keys off — so scheduling one here would flag every GNet trip
    as unacknowledged forever with no way to clear it.
    """
    from apps.dispatch.models import Assignment

    res = assignment.reservation
    if assignment.is_in_house or assignment.channel == Assignment.Channel.GNET:
        return
    if res.pickup_at is None:
        return
    _ensure(res, TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE, res.pickup_at)


def cancel_affiliate_confirmation(reservation) -> int:
    """The affiliate no longer holds the trip (declined / withdrawn / released)."""
    return TouchPoint.objects.filter(
        reservation=reservation,
        kind=TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE,
        status=TouchPoint.Status.SCHEDULED,
    ).update(status=TouchPoint.Status.CANCELLED)


def _driver_message_ready(reservation, assignment) -> bool:
    """The send-time conditions: a Dispatched-or-later status and the confirmed coverage's
    required driver details all on file."""
    from apps.dispatch.models import DispatchAlertConfig
    from apps.dispatch.services import driver_details_ready

    if reservation.trip_status not in DRIVER_MESSAGE_STATUSES:
        return False
    return assignment is not None and driver_details_ready(assignment, DispatchAlertConfig.load())


def sync_driver_message(reservation) -> TouchPoint | None:
    """Queue, or re-date, the driver message once the trip is Dispatched with details ready.

    Called after every change that can alter those facts (status, driver info, coverage,
    pickup). It sends at `max(now, pickup + the message's timing offset)`: early dispatches
    wait for the window, late ones go on the next tick. A SCHEDULED row follows the pickup
    even while it is held. A message already SENT is left alone. Returns the row created
    or re-dated.
    """
    from django.db import transaction

    from apps.dispatch.models import Assignment
    from apps.reservations.models import Reservation

    kind = TouchPoint.Kind.DRIVER_RELEASED
    pickup = reservation.pickup_at
    now = timezone.now()
    if pickup is None or pickup <= now:
        return None
    waiting = TouchPoint.objects.filter(
        reservation=reservation, kind=kind, status=TouchPoint.Status.SCHEDULED
    ).exists()
    if reservation.trip_status not in DRIVER_MESSAGE_STATUSES and not waiting:
        return None
    with transaction.atomic():
        # The webhook and a dispatcher can land together; the row lock serialises them so
        # the customer gets one message.
        locked = Reservation.objects.select_for_update().get(pk=reservation.pk)
        existing = (
            TouchPoint.objects.filter(reservation=locked, kind=kind)
            .exclude(status=TouchPoint.Status.CANCELLED)
            .first()
        )
        if existing is not None and existing.status == TouchPoint.Status.SENT:
            return _maybe_update(locked, existing)
        if existing is not None and existing.status != TouchPoint.Status.SCHEDULED:
            return None
        rows = overrides.load()
        at = max(now, pickup + overrides.effective(kind, rows).offset)
        if existing is not None:
            if existing.scheduled_for != at:
                existing.scheduled_for = at
                existing.save(update_fields=["scheduled_for", "updated_at"])
            return existing
        if not overrides.is_enabled(kind, rows):
            return None
        coverage = locked.assignments.filter(status=Assignment.Status.CONFIRMED).first()
        if not _driver_message_ready(locked, coverage):
            return None
        return _ensure(locked, kind, pickup, at=at, rows=rows)


def _maybe_update(reservation, sent: TouchPoint) -> TouchPoint | None:
    """Queue a "driver details updated" message when the ready details differ from the last
    ones the customer was sent.

    "Last sent" is the newest SENT row among the driver message and its updates. A row sent
    before details were recorded is unknown, so it never triggers an update. One SCHEDULED
    update at a time: it reads the details current at send time. Caller holds the lock.
    """
    from apps.dispatch.models import DispatchAlertConfig
    from apps.dispatch.services import driver_details_ready

    kind = TouchPoint.Kind.DRIVER_UPDATED
    rows = overrides.load()
    if not overrides.is_enabled(kind, rows):
        return None
    if reservation.trip_status not in DRIVER_MESSAGE_STATUSES:
        return None
    assignment = _confirmed_assignment(reservation)
    if assignment is None or not driver_details_ready(assignment, DispatchAlertConfig.load()):
        return None
    newest = max(
        TouchPoint.objects.filter(
            reservation=reservation, kind__in=_DRIVER_KINDS, status=TouchPoint.Status.SENT
        ),
        key=lambda tp: (tp.sent_at or tp.created_at, tp.pk),
        default=sent,
    )
    if newest.driver_details is None or newest.driver_details == assignment.driver_info:
        return None
    if TouchPoint.objects.filter(
        reservation=reservation, kind=kind, status=TouchPoint.Status.SCHEDULED
    ).exists():
        return None
    return TouchPoint.objects.create(
        lead=reservation.lead, reservation=reservation, kind=kind, scheduled_for=timezone.now()
    )


def notify_status_change(reservation, status: str) -> None:
    """Map a trip-status transition to its customer message (APC-22), if configured on."""
    kind = _STATUS_KINDS.get(status)
    if kind is None:
        return
    _ensure(reservation, kind, timezone.now())


def trigger_auth_expired(lead) -> TouchPoint | None:
    """The customer's held deposit was released before we confirmed (APC-26).

    Lead-level, not reservation-anchored — the order never became trips anyone is running.

    Blocks only on an *undelivered* row, deliberately: a customer who re-authorizes and
    then lapses a second time has to be told a second time. The sweep can't double-fire
    within one lapse anyway — it only walks AUTHORIZED charges, and expiring one is what
    takes it out of that set.
    """
    kind = TouchPoint.Kind.ORDER_AUTH_EXPIRED
    undelivered = TouchPoint.objects.filter(
        lead=lead, kind=kind, status=TouchPoint.Status.SCHEDULED
    ).exists()
    if undelivered:
        return None
    return _create(lead, kind, timezone.now())


def trigger_order_decided(lead, *, confirmed: bool) -> TouchPoint | None:
    """Tell the customer how the wait ended (APC-26 step 9).

    One function for both outcomes because they are the same event from the customer's
    side — the answer they've been waiting on — and keeping them together makes it hard to
    ship one without the other.
    """
    kind = TouchPoint.Kind.ORDER_CONFIRMED if confirmed else TouchPoint.Kind.ORDER_CANCELLED
    if TouchPoint.objects.filter(lead=lead, kind=kind, status=TouchPoint.Status.SCHEDULED).exists():
        return None
    return _create(lead, kind, timezone.now())


def cancel_pending(lead, *, kinds=None) -> int:
    """Cancel SCHEDULED touch-points for a lead.

    Defaults to every kind except review_request, thank_you, payment_reminder, and the
    reservation-anchored service-date kinds (all fire on a booked lead by design); pass
    explicit ``kinds`` to override, e.g. cancelling everything on LOST.
    """
    qs = TouchPoint.objects.filter(lead=lead, status=TouchPoint.Status.SCHEDULED)
    if kinds is None:
        qs = qs.exclude(
            kind__in=(
                TouchPoint.Kind.REVIEW_REQUEST,
                TouchPoint.Kind.THANK_YOU,
                TouchPoint.Kind.PAYMENT_REMINDER,
                *_RESERVATION_KINDS,
            )
        )
    else:
        qs = qs.filter(kind__in=kinds)
    return qs.update(status=TouchPoint.Status.CANCELLED)


def _confirmed_assignment(reservation):
    """The trip's active coverage — the one shared lookup, see `dispatch.selectors`."""
    if reservation is None:
        return None
    from apps.dispatch.selectors import confirmed_assignment

    return confirmed_assignment(reservation)
