"""Touch-point scheduling + sending (spec 2026-07-11, client cadence TP1-TP8)."""

import logging
from datetime import datetime, time

from django.conf import settings
from django.utils import timezone

from apps.integrations import podium
from apps.integrations.la_sync import _split_name
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected

from .models import Review, TouchPoint
from .touchpoint_templates import TEMPLATES, build_context, render

logger = logging.getLogger(__name__)

# Podium calls the SMS channel "phone" (apps/messaging/views.py does the same mapping).
PODIUM_CHANNEL = {"sms": "phone", "email": "email"}

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
    {_K.WED_FINAL_DETAILS, _K.TRIP_CONFIRM_CUSTOMER, _K.TRIP_CONFIRM_AFFILIATE}
)
_TRIGGERED_KINDS = frozenset(
    {_K.DRIVER_RELEASED, _K.STATUS_DISPATCHED, _K.STATUS_ON_THE_WAY, _K.STATUS_ARRIVED}
)
_RESERVATION_KINDS = _SERVICE_DATE_KINDS | _TRIGGERED_KINDS

# Trip status -> the customer notification kind it fires (APC-22).
_STATUS_KINDS = {
    "dispatched": _K.STATUS_DISPATCHED,
    "on_the_way": _K.STATUS_ON_THE_WAY,
    "arrived": _K.STATUS_ARRIVED,
}


def _create(lead, kind: str, anchor_dt) -> TouchPoint:
    return TouchPoint.objects.create(
        lead=lead, kind=kind, scheduled_for=anchor_dt + TEMPLATES[kind].offset
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
        if expires + TEMPLATES[TouchPoint.Kind.TP7_EXPIRING].offset > timezone.now():
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


def schedule_payment_reminder(lead) -> None:
    """The 72-hour pre-charge notice (spec 2026-08-30 §9). One row per lead.

    A pre-charge notice, not dunning: it only makes sense for a booked lead with a card on
    file whose balance the cron will auto-charge, so it is a no-op without a card or a dated
    trip. Called from `sync_plan_from_collected` when the balance flips to SCHEDULED. The
    anchor is midnight on `balance_due_date` in the project timezone — `Reservation` has no
    per-trip zone yet (see the trip-timezone spec); revisit together with that work.
    """
    kind = TouchPoint.Kind.PAYMENT_REMINDER
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


def _ensure(reservation, kind: str, anchor_dt) -> TouchPoint | None:
    """Create one reservation-anchored row unless a non-CANCELLED one already exists.

    A SENT / SKIPPED / FAILED row still blocks — the customer already got it, or we already
    decided not to send. Only a CANCELLED row (a supersede) allows a fresh one.
    """
    blocking = (
        TouchPoint.objects.filter(reservation=reservation, kind=kind)
        .exclude(status=TouchPoint.Status.CANCELLED)
        .exists()
    )
    if blocking:
        return None
    return TouchPoint.objects.create(
        lead=reservation.lead,
        reservation=reservation,
        kind=kind,
        scheduled_for=anchor_dt + TEMPLATES[kind].offset,
    )


def _is_wedding(reservation) -> bool:
    """A leg the wedding builder generated, or a trip whose service type is the wedding one.
    Same test the wedding builder uses (`apps.public.services.WEDDING_SERVICE_NAME`)."""
    from apps.public.services import WEDDING_SERVICE_NAME

    if reservation.source_leg_id:
        return True
    st = reservation.service_type
    return bool(st and st.name.strip().lower() == WEDDING_SERVICE_NAME.lower())


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
        if _is_wedding(res):
            _ensure(res, TouchPoint.Kind.WED_FINAL_DETAILS, res.pickup_at)


def reschedule_service_touchpoints(reservation) -> None:
    """Pickup date/time changed — cancel this trip's pending service-date rows and rebuild."""
    from apps.dispatch.models import Assignment

    TouchPoint.objects.filter(
        reservation=reservation,
        kind__in=_SERVICE_DATE_KINDS,
        status=TouchPoint.Status.SCHEDULED,
    ).update(status=TouchPoint.Status.CANCELLED)
    schedule_service_touchpoints(reservation.lead)
    still_covered = reservation.assignments.filter(status=Assignment.Status.CONFIRMED).exists()
    if still_covered and reservation.pickup_at is not None:
        _ensure(reservation, TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE, reservation.pickup_at)


def schedule_affiliate_confirmation(assignment) -> None:
    """T-48h affiliate confirmation, scheduled when an affiliate confirms coverage (APC-20)."""
    res = assignment.reservation
    if assignment.is_in_house or res.pickup_at is None:
        return
    _ensure(res, TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE, res.pickup_at)


def cancel_affiliate_confirmation(reservation) -> int:
    """The affiliate no longer holds the trip (declined / withdrawn / released)."""
    return TouchPoint.objects.filter(
        reservation=reservation,
        kind=TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE,
        status=TouchPoint.Status.SCHEDULED,
    ).update(status=TouchPoint.Status.CANCELLED)


def trigger_driver_released(assignment) -> None:
    """Driver info was entered on a confirmed trip — release it to the customer (APC-21).

    Trigger-based: the row is created with `scheduled_for = now` and delivered by
    `run_touchpoints` on the next tick, keeping one send path.
    """
    from apps.dispatch.models import Assignment

    if assignment.status != Assignment.Status.CONFIRMED or not assignment.has_driver_info:
        return
    if not _config().is_enabled(TouchPoint.Kind.DRIVER_RELEASED):
        return
    _ensure(assignment.reservation, TouchPoint.Kind.DRIVER_RELEASED, timezone.now())


def notify_status_change(reservation, status: str) -> None:
    """Map a trip-status transition to its customer message (APC-22), if configured on."""
    kind = _STATUS_KINDS.get(status)
    if kind is None or not _config().is_enabled(kind):
        return
    _ensure(reservation, kind, timezone.now())


def _config():
    from .models import NotificationConfig

    return NotificationConfig.load()


def cancel_pending(lead, *, kinds=None) -> int:
    """Cancel SCHEDULED touch-points for a lead.

    Defaults to every kind except review_request, payment_reminder, and the
    reservation-anchored service-date kinds (all fire on a booked lead by design); pass
    explicit ``kinds`` to override, e.g. cancelling everything on LOST.
    """
    qs = TouchPoint.objects.filter(lead=lead, status=TouchPoint.Status.SCHEDULED)
    if kinds is None:
        qs = qs.exclude(
            kind__in=(
                TouchPoint.Kind.REVIEW_REQUEST,
                TouchPoint.Kind.PAYMENT_REMINDER,
                *_RESERVATION_KINDS,
            )
        )
    else:
        qs = qs.filter(kind__in=kinds)
    return qs.update(status=TouchPoint.Status.CANCELLED)


def _mark(tp: TouchPoint, *, status: str, error: str = "") -> None:
    tp.status = status
    tp.error = error[:255]
    tp.save(update_fields=["status", "error", "updated_at"])


def _channel_identifier(contact, channel: str) -> str:
    if channel == "email":
        return (contact.email or "").strip()
    if channel == "sms":
        return (contact.phone or "").strip()
    return ""


def _render_body(template, channel: str, ctx: dict) -> str:
    if channel == "email":
        subject = render(template.subject, ctx) if template.subject else ""
        body = render(template.email_body, ctx) if template.email_body else ""
        return f"{subject}\n\n{body}" if subject else body
    return render(template.sms_body, ctx)


def _fill_links(tp: TouchPoint, ctx: dict, base_url: str) -> None:
    """Populate the public-page link vars this kind's body uses."""
    from apps.leads.services import make_pay_page_url, make_quote_page_url
    from apps.reservations.acknowledgements import (
        affiliate_ack_url,
        trip_ack_url,
        wedding_details_url,
    )

    ctx["quote_link"] = make_quote_page_url(tp.lead, base_url=base_url)
    ctx["pay_link"] = make_pay_page_url(tp.lead, base_url=base_url)
    res = tp.reservation
    if res is None:
        return
    if tp.kind == TouchPoint.Kind.WED_FINAL_DETAILS:
        ctx["confirm_link"] = wedding_details_url(res, base_url=base_url)
    elif tp.kind == TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE:
        from apps.dispatch.models import Assignment

        a = res.assignments.filter(status=Assignment.Status.CONFIRMED).first()
        if a is not None:
            ctx["ack_link"] = affiliate_ack_url(a, base_url=base_url)
    else:
        ctx["confirm_link"] = trip_ack_url(res, base_url=base_url)


def _send(tp: TouchPoint, template, available: dict[str, str]) -> bool:
    """Send over each available channel; SENT on any success, else FAILED."""
    base_url = settings.PUBLIC_BASE_URL or ""
    ctx = build_context(tp.lead, tp.reservation)
    _fill_links(tp, ctx, base_url)

    first_uid = ""
    any_success = False
    errors: list[str] = []
    for channel, identifier in available.items():
        body = _render_body(template, channel, ctx)
        try:
            resp = podium.send_message(
                identifier=identifier,
                channel_type=PODIUM_CHANNEL.get(channel, channel),
                body=body,
            )
            if not first_uid:
                first_uid = resp.get("uid") or resp.get("data", {}).get("uid", "")
            any_success = True
        except (PodiumAPIError, PodiumNotConnected) as exc:
            errors.append(f"{channel}: {exc}")

    if any_success:
        tp.status = TouchPoint.Status.SENT
        tp.sent_at = timezone.now()
        tp.podium_message_uid = first_uid
        tp.error = ""
        tp.save(update_fields=["status", "sent_at", "podium_message_uid", "error", "updated_at"])
        return True
    _mark(tp, status=TouchPoint.Status.FAILED, error="; ".join(errors))
    return False


def _send_review_invite(tp: TouchPoint) -> bool:
    """Create the Podium review invitation, log a Review row, and SMS the link."""
    lead = tp.lead
    contact = lead.contact
    phone = (contact.phone or "").strip()
    if not phone:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="no phone for review invite")
        return False

    first, last = _split_name(contact.name)
    try:
        invite = podium.create_review_invitation(phone=phone, first_name=first, last_name=last)
    except (PodiumAPIError, PodiumNotConnected) as exc:
        _mark(tp, status=TouchPoint.Status.FAILED, error=str(exc))
        return False

    link = invite.get("url") or invite.get("link") or ""
    uid = invite.get("uid", "")
    Review.objects.create(
        lead=lead,
        contact=contact,
        podium_review_invite_uid=uid,
        delivery_status=Review.DeliveryStatus.SENT,
        requested_at=timezone.now(),
    )

    template = TEMPLATES[tp.kind]
    ctx = build_context(lead)
    ctx["review_link"] = link
    body = render(template.sms_body, ctx)
    try:
        resp = podium.send_message(identifier=phone, channel_type=PODIUM_CHANNEL["sms"], body=body)
        tp.status = TouchPoint.Status.SENT
        tp.sent_at = timezone.now()
        tp.podium_message_uid = resp.get("uid") or resp.get("data", {}).get("uid", "")
        tp.error = ""
        tp.save(update_fields=["status", "sent_at", "podium_message_uid", "error", "updated_at"])
        return True
    except (PodiumAPIError, PodiumNotConnected) as exc:
        _mark(tp, status=TouchPoint.Status.FAILED, error=str(exc))
        return False


_LINK_KINDS = frozenset(
    {
        _K.WED_FINAL_DETAILS,
        _K.TRIP_CONFIRM_CUSTOMER,
        _K.TRIP_CONFIRM_AFFILIATE,
        _K.STATUS_DISPATCHED,
    }
)


def _recipient(tp: TouchPoint, template):
    """The message's addressee — `lead.contact`, or the confirmed affiliate's vendor."""
    if template.audience == "affiliate":
        from apps.dispatch.models import Assignment

        res = tp.reservation
        a = (
            res.assignments.filter(status=Assignment.Status.CONFIRMED)
            .select_related("vendor")
            .first()
            if res is not None
            else None
        )
        return a.vendor if a is not None and a.vendor_id else None
    return tp.lead.contact


def _process(tp: TouchPoint) -> bool:
    """Evaluate skip conditions at send time and send. Returns True iff SENT."""
    lead = tp.lead
    kind = tp.kind

    if lead.status == lead.Status.LOST:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead LOST")
        return False

    if kind == TouchPoint.Kind.REVIEW_REQUEST:
        return _send_review_invite(tp)

    if kind in _RESERVATION_KINDS:
        from apps.dispatch.selectors import CANCELLED_STATUSES

        if lead.status != lead.Status.BOOKED:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead not booked")
            return False
        if tp.reservation is None:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="no reservation")
            return False
        if tp.reservation.trip_status in CANCELLED_STATUSES:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="trip cancelled")
            return False
        if kind in _SERVICE_DATE_KINDS and tp.reservation.pickup_at is None:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="trip has no pickup date")
            return False
        if not _config().is_enabled(kind):
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="message disabled")
            return False
        if kind in _LINK_KINDS and not settings.PUBLIC_BASE_URL:
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False
        # fall through to the shared template / recipient / _send path
    elif kind == TouchPoint.Kind.PAYMENT_REMINDER:
        from apps.payments.models import PaymentPlan
        from apps.payments.services import remaining_balance

        plan = getattr(lead, "payment", None)
        if plan is None or not plan.stripe_payment_method_id:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="no card on file")
            return False
        if plan.balance_status != PaymentPlan.BalanceStatus.SCHEDULED:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="balance not scheduled")
            return False
        if remaining_balance(lead) <= 0:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="nothing owed")
            return False
        if not settings.PUBLIC_BASE_URL:
            # Config isn't ready — leave SCHEDULED (matches the quote kinds).
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False
        # fall through to the shared template / channel / _send path
    elif lead.status == lead.Status.BOOKED:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead BOOKED")
        return False

    if kind in _QUOTE_KINDS:
        plan = getattr(lead, "payment", None)
        if plan is not None and plan.deposit_status == plan.DepositStatus.PAID:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="deposit already PAID")
            return False
        if not settings.PUBLIC_BASE_URL:
            # Config isn't ready yet — leave the row SCHEDULED so it sends once
            # PUBLIC_BASE_URL is set, instead of silently SKIPPED/FAILED.
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False

    template = TEMPLATES[kind]
    recipient = _recipient(tp, template)
    if recipient is None:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="no confirmed affiliate")
        return False
    available = {
        channel: identifier
        for channel in template.channels
        if (identifier := _channel_identifier(recipient, channel))
    }
    if not available:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="recipient has no usable channel")
        return False

    return _send(tp, template, available)


def run_touchpoints() -> int:
    """Send every due SCHEDULED touch-point. Returns the count SENT."""
    if not settings.TOUCHPOINTS_ENABLED:
        return 0

    due = (
        TouchPoint.objects.filter(
            status=TouchPoint.Status.SCHEDULED, scheduled_for__lte=timezone.now()
        )
        .select_related(
            "lead",
            "lead__contact",
            "reservation",
            "reservation__vehicle",
            "reservation__service_type",
        )
        .prefetch_related("reservation__stops", "reservation__assignments__vendor")
    )

    sent = 0
    for tp in due:
        try:
            if _process(tp):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad row must not kill the run
            logger.exception("touch-point %s failed to send", tp.pk)
            _mark(tp, status=TouchPoint.Status.FAILED, error="unexpected error")
    return sent
