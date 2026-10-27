"""Touch-point scheduling + sending (spec 2026-07-11, client cadence TP1-TP8)."""

import logging
from datetime import datetime, time

from django.conf import settings
from django.utils import timezone

from apps.core import outbound
from apps.integrations import podium
from apps.integrations.la_sync import _split_name
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected

from . import overrides
from .models import NotificationConfig, Review, TouchPoint
from .touchpoint_templates import build_context, render

logger = logging.getLogger(__name__)

# Podium calls the SMS channel "phone" (apps/messaging/views.py does the same mapping).
PODIUM_CHANNEL = {"sms": "phone", "email": "email"}

# Why a due row was closed unsent while the go-live switch was off (APC-90).
OUTBOUND_OFF = "outbound messaging off"

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
    {_K.DRIVER_RELEASED, _K.STATUS_DISPATCHED, _K.STATUS_ON_THE_WAY, _K.STATUS_ARRIVED}
)
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


def _ensure(reservation, kind: str, anchor_dt, *, at: datetime | None = None) -> TouchPoint | None:
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
    if blocking or not overrides.is_enabled(kind):
        return None
    return TouchPoint.objects.create(
        lead=reservation.lead,
        reservation=reservation,
        kind=kind,
        scheduled_for=at or anchor_dt + overrides.effective(kind).offset,
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


# Trip statuses at which the customer may be told who is driving: from Dispatched onward.
DRIVER_MESSAGE_STATUSES = frozenset(
    {
        "dispatched",
        "dispatched_non_la",
        "on_the_way",
        "circling",
        "arrived",
        "customer_in_car",
    }
)


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
    wait for the window, late ones go on the next tick. A message already SENT is left
    alone. Returns the row created or re-dated.
    """
    from apps.dispatch.models import Assignment

    kind = TouchPoint.Kind.DRIVER_RELEASED
    pickup = reservation.pickup_at
    now = timezone.now()
    if pickup is None or pickup <= now or not overrides.is_enabled(kind):
        return None
    # Read coverage fresh: the caller may hold a prefetch from before the change.
    coverage = reservation.assignments.filter(status=Assignment.Status.CONFIRMED).first()
    if not _driver_message_ready(reservation, coverage):
        return None
    existing = (
        TouchPoint.objects.filter(reservation=reservation, kind=kind)
        .exclude(status=TouchPoint.Status.CANCELLED)
        .first()
    )
    at = max(now, pickup + overrides.effective(kind).offset)
    if existing is None:
        return _ensure(reservation, kind, pickup, at=at)
    if existing.status != TouchPoint.Status.SCHEDULED:
        return None
    if existing.scheduled_for != at:
        existing.scheduled_for = at
        existing.save(update_fields=["scheduled_for", "updated_at"])
    return existing


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
        trip_day_ack_url,
        wedding_day_ack_url,
        wedding_details_url,
    )

    ctx["quote_link"] = make_quote_page_url(tp.lead, base_url=base_url)
    ctx["pay_link"] = make_pay_page_url(tp.lead, base_url=base_url)
    res = tp.reservation
    if tp.kind == TouchPoint.Kind.THANK_YOU:
        # The trip page for the order's last day — once the order has finished that page
        # is the rating form (APC-63).
        days = [r.pickup_date for r in tp.lead.reservations.all() if r.pickup_date]
        if days:
            ctx["confirm_link"] = trip_day_ack_url(tp.lead.contact, max(days), base_url=base_url)
        return
    if res is None:
        return
    if tp.kind == TouchPoint.Kind.WED_FINAL_DETAILS:
        ctx["confirm_link"] = wedding_details_url(res, base_url=base_url)
    elif tp.kind == TouchPoint.Kind.TRIP_CONFIRM_AFFILIATE:
        a = _confirmed_assignment(res)
        if a is not None:
            ctx["ack_link"] = affiliate_ack_url(a, base_url=base_url)
    elif tp.kind in _CUSTOMER_CONFIRM_KINDS and _is_wedding(res):
        ctx["confirm_link"] = wedding_day_ack_url(tp.lead, res.pickup_date, base_url=base_url)
    else:
        ctx["confirm_link"] = trip_day_ack_url(tp.lead.contact, res.pickup_date, base_url=base_url)


def _send(
    tp: TouchPoint, template, available: dict[str, str], *, group=None, recipient=None
) -> bool:
    """Send over each available channel; SENT on any success, else FAILED.

    ``group`` is the customer's whole day (APC-19) — its trip sheets replace the anchor
    reservation's single sheet so one message covers every trip.
    """
    base_url = settings.PUBLIC_BASE_URL or ""
    ctx = build_context(tp.lead, tp.reservation)
    if recipient is not None and getattr(recipient, "pk", None) != tp.lead.contact_id:
        # Addressed to someone other than the booker (a wedding's billing contact, APC-94):
        # greet the person who receives it.
        if template.audience != "affiliate":
            ctx["first_name"] = (recipient.name or "").split(" ")[0] or "there"
    if group:
        from apps.reservations.services import trip_sheet_text

        ctx["trip_sheet"] = "\n\n".join(trip_sheet_text(t) for t in group)
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
        fields = ["status", "sent_at", "podium_message_uid", "error", "updated_at"]
        if tp.kind == TouchPoint.Kind.DRIVER_RELEASED:
            tp.driver_details = _confirmed_assignment(tp.reservation).driver_info
            fields.append("driver_details")
        tp.save(update_fields=fields)
        return True
    _mark(tp, status=TouchPoint.Status.FAILED, error="; ".join(errors))
    return False


def _send_review_invite(tp: TouchPoint, rows=None) -> bool:
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

    template = overrides.effective(tp.kind, rows)
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
        _K.TRIP_CONFIRM_CUSTOMER_2,
        _K.TRIP_CONFIRM_AFFILIATE,
        _K.STATUS_DISPATCHED,
        # Both carry a link the customer is meant to act on; sending one with a bare path
        # burns the only message they get (APC-26).
        _K.ORDER_AUTH_EXPIRED,
        _K.ORDER_CONFIRMED,
    }
)


def _review_hold_reason(lead) -> str:
    """Why this order's review request shouldn't go out (decision D3), or "". We don't ask
    an unhappy customer for a public review — a person follows up instead."""
    from apps.leads.models import CustomerFeedback
    from apps.reservations.models import TripIssue

    if TripIssue.objects.filter(
        review__reservation__lead=lead, category=TripIssue.Category.COMPLAINT
    ).exists():
        return "complaint logged"
    feedback = CustomerFeedback.objects.filter(lead=lead).first()
    if feedback is not None and feedback.is_low:
        return "low rating"
    return ""


def _thank_you_template(lead, rows=None):
    from apps.reservations.services import is_wedding_trip

    wedding = bool(lead.wedding_name) or any(is_wedding_trip(r) for r in lead.reservations.all())
    return overrides.effective("thank_you_wedding" if wedding else TouchPoint.Kind.THANK_YOU, rows)


def _confirmed_assignment(reservation):
    """The trip's active coverage — the one shared lookup, see `dispatch.selectors`."""
    if reservation is None:
        return None
    from apps.dispatch.selectors import confirmed_assignment

    return confirmed_assignment(reservation)


def _customer_confirm_group(tp: TouchPoint):
    """The trips one customer-confirmation covers. A wedding's notice covers its own order
    that day (it goes to the billing contact with the fee acknowledgment, APC-94); any
    other notice covers the rest of the booker's day, wedding trips left out, so the two
    never stand in for each other in `_already_covered`."""
    from apps.reservations.services import trip_day_group, wedding_day_group

    day = tp.reservation.pickup_date
    if _is_wedding(tp.reservation):
        return wedding_day_group(tp.lead, day)
    return [t for t in trip_day_group(tp.lead.contact, day) if not _is_wedding(t)]


def _already_covered(tp: TouchPoint, group) -> bool:
    """A sibling row of the same wave already sent the grouped notice for this day.

    Rows are scheduled per reservation (that is what `reschedule_service_touchpoints`
    cancels and rebuilds); whichever comes due first sends for the whole day and the rest
    stand down here, so the customer gets one message per wave rather than one per trip.
    """
    return (
        TouchPoint.objects.filter(
            kind=tp.kind, status=TouchPoint.Status.SENT, reservation__in=[t.pk for t in group]
        )
        .exclude(pk=tp.pk)
        .exists()
    )


def _recipient(tp: TouchPoint, template):
    """The message's addressee — `lead.contact`, or the confirmed affiliate's vendor.

    APC-94: a wedding's trip confirmations go to the **billing contact** (per-order
    override → company billing contact → booking contact), because that page carries the
    cleaning-fee / overtime acknowledgment the client wants from whoever pays, and a
    planner's tick must never count as the customer's.
    """
    if template.audience == "affiliate":
        a = _confirmed_assignment(tp.reservation)
        return a.vendor if a is not None and a.vendor_id else None
    if (
        tp.kind in _CUSTOMER_CONFIRM_KINDS
        and tp.reservation is not None
        and _is_wedding(tp.reservation)
    ):
        return tp.lead.effective_billing_contact
    return tp.lead.contact


def _process(tp: TouchPoint, rows=None, master: bool | None = None) -> bool:
    """Evaluate skip conditions at send time and send. Returns True iff SENT.

    ``rows`` is the batch's `MessageTemplate` overrides and ``master`` the master switch;
    `run_touchpoints` loads both once for the whole batch (each is loaded here when None).
    """
    lead = tp.lead
    kind = tp.kind
    group = None

    rows = overrides.load() if rows is None else rows
    if not overrides.is_enabled(kind, rows, master=master):
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="message disabled")
        return False

    # ORDER_CANCELLED is the one message that exists *because* the lead is lost — telling
    # a customer we released their booking is not something to skip for being lost.
    if lead.status == lead.Status.LOST and kind != TouchPoint.Kind.ORDER_CANCELLED:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead LOST")
        return False

    if kind == TouchPoint.Kind.REVIEW_REQUEST:
        reason = _review_hold_reason(lead)
        if reason:
            _mark(tp, status=TouchPoint.Status.CANCELLED, error=f"Review request held: {reason}")
            # A complaint's follow-up is a task (complaint_followup); make sure it exists
            # now rather than on the next run-tasks tick. A low rating has no task — it is
            # picked up by the customer review report.
            from apps.tasks import services as tasks

            tasks.sync(lead)
            return False
        return _send_review_invite(tp, rows)

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
        if kind == TouchPoint.Kind.DRIVER_RELEASED:
            pickup = tp.reservation.pickup_at
            if pickup is not None and pickup <= timezone.now():
                _mark(tp, status=TouchPoint.Status.SKIPPED, error="pickup passed")
                return False
            if not _driver_message_ready(tp.reservation, _confirmed_assignment(tp.reservation)):
                return False  # held: stays SCHEDULED, retried every tick
        if kind in _SERVICE_DATE_KINDS and tp.reservation.pickup_at is None:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="trip has no pickup date")
            return False
        if kind in _LINK_KINDS and not settings.PUBLIC_BASE_URL:
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False
        if kind in _CUSTOMER_CONFIRM_KINDS:
            group = _customer_confirm_group(tp)
            fees_owed = _is_wedding(tp.reservation) and lead.fees_acknowledged_at is None
            if group and all(t.customer_confirmed_at is not None for t in group) and not fees_owed:
                _mark(tp, status=TouchPoint.Status.SKIPPED, error="day already confirmed")
                return False
            if _already_covered(tp, group):
                _mark(tp, status=TouchPoint.Status.SKIPPED, error="covered by the grouped notice")
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
    elif kind == TouchPoint.Kind.THANK_YOU:
        if lead.status != lead.Status.BOOKED:
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead not booked")
            return False
        if not settings.PUBLIC_BASE_URL:
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False
        # fall through
    elif kind in _ORDER_KINDS:
        # Lead-level outcome messages (APC-26). They deliberately sit outside the
        # status-based skips: `order_confirmed` fires on a lead that is now BOOKED, and
        # `order_cancelled` on one that is now LOST — the very states those skips exist to
        # stop the *quote* program in.
        if kind in _LINK_KINDS and not settings.PUBLIC_BASE_URL:
            # These messages are mostly "here is the link" — without a base URL the link
            # renders as a bare path and the customer gets a dead one, permanently, because
            # the row would be marked SENT. Leave it SCHEDULED like the quote kinds.
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False
        # fall through
    elif lead.status == lead.Status.BOOKED:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead BOOKED")
        return False
    elif lead.status == lead.Status.ENGAGED:
        # They authorized and are waiting on us (APC-26) — chasing them to book a quote
        # they have already paid for is the worst message this system could send.
        _mark(tp, status=TouchPoint.Status.SKIPPED, error="lead ENGAGED")
        return False

    if kind in _QUOTE_KINDS:
        plan = getattr(lead, "payment", None)
        if plan is not None and plan.deposit_status in (
            plan.DepositStatus.PAID,
            plan.DepositStatus.AUTHORIZED,
        ):
            _mark(tp, status=TouchPoint.Status.SKIPPED, error="deposit already taken")
            return False
        if not settings.PUBLIC_BASE_URL:
            # Config isn't ready yet — leave the row SCHEDULED so it sends once
            # PUBLIC_BASE_URL is set, instead of silently SKIPPED/FAILED.
            logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, kind)
            return False

    if rows is None:
        rows = overrides.load()
    if kind == TouchPoint.Kind.THANK_YOU:
        template = _thank_you_template(lead, rows)
    else:
        template = overrides.effective(kind, rows)
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

    return _send(tp, template, available, group=group, recipient=recipient)


def run_touchpoints() -> int:
    """Send every due SCHEDULED touch-point. Returns the count SENT.

    With the engine on but the go-live switch off (APC-90), each due row is closed as
    SKIPPED with the reason rather than left SCHEDULED: left alone they would pile up and
    all send at once, weeks stale, the moment the switch flips. Rows not yet due are
    untouched, so whatever falls due after go-live sends normally.
    """
    if not settings.TOUCHPOINTS_ENABLED:
        return 0

    due = TouchPoint.objects.filter(
        status=TouchPoint.Status.SCHEDULED, scheduled_for__lte=timezone.now()
    )
    if not outbound.enabled():
        held = due.update(
            status=TouchPoint.Status.SKIPPED, error=OUTBOUND_OFF, updated_at=timezone.now()
        )
        if held:
            logger.info("outbound off: %d due touch-point(s) skipped, not sent", held)
        return 0

    due = due.select_related(
        "lead",
        "lead__contact",
        "reservation",
        "reservation__vehicle",
        "reservation__service_type",
    ).prefetch_related(
        "reservation__stops",
        "reservation__assignments__vendor",
        "reservation__assignments__driver",
        "reservation__assignments__vehicle",
    )

    master = NotificationConfig.load().enabled
    rows = overrides.load()
    sent = 0
    for tp in due:
        try:
            if _process(tp, rows=rows, master=master):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad row must not kill the run
            logger.exception("touch-point %s failed to send", tp.pk)
            _mark(tp, status=TouchPoint.Status.FAILED, error="unexpected error")
    return sent
