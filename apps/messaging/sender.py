"""The shared sender (spec C sections 5 and 9): one way out for every automated message.

`deliver(tp)` asks `rules.check` whether a due TouchPoint may go, resolves who gets it at
send time, and makes one `Delivery` per person per channel: sent, failed with the provider's
error, or skipped with the reason ("No mobile on file", "Already sent for Nov 14"). The
TouchPoint keeps today's status and error strings, which the order's history and the golden
recording read. `run_touchpoints` is the `run-touchpoints` cron job; `send_due` is the same
batch for rows an event queued, called once its transaction commits. `send_now` is the same
path for a staff click, an invite or a report, sent inside the request (spec C section 8).

Transport is today's: texts and customer/affiliate email through Podium, planner and staff
email (and any trigger whose email goes by Postmark) through our own mail, in the branded
layout (`rendering`). The review request keeps Podium's review invite. `OUTBOUND_ENABLED` is
enforced inside both send primitives; `TOUCHPOINTS_ENABLED` and the master switch gate the
batch as they always have, and never a click.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import time, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core import outbound
from apps.integrations import podium
from apps.integrations.la_sync import _split_name
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.notifications import email as mail

from . import fields, recipients, rendering, rules, triggers
from .models import Delivery, MessageTemplate, NotificationConfig, Review, TouchPoint
from .recipients import Recipient
from .rendering import Block
from .touchpoint_templates import build_context, render

logger = logging.getLogger(__name__)

# Podium calls the SMS channel "phone".
PODIUM_CHANNEL = {"sms": "phone", "email": "email"}
# Why a due row was closed unsent while the go-live switch was off (APC-90).
OUTBOUND_OFF = "outbound messaging off"
NO_RECIPIENT = "No recipient"
# Today's TouchPoint error when the people it resolved have no address it can use.
NO_CHANNEL = "recipient has no usable channel"
MISSING = {"sms": "No mobile on file", "email": "No email on file"}
# Why the cron closed a staff click's or report's TouchPoint that its own send never finished.
INTERRUPTED = "send interrupted"
# Trigger kinds sent only by `send_now`, inside the click or the report's cron job.
_SENT_NOW = frozenset({"staff_click", "schedule"})

_A = MessageTemplate.Audience
_POSTMARK_AUDIENCES = frozenset({_A.PLANNER, _A.STAFF})
# Triggers whose send records the driver details it carried, so a later change can be
# compared against what the customer was told (`events.sync_waiting`).
_DRIVER_TRIGGERS = frozenset({"pickup_dispatched", "driver_changed"})


class _NotSent(Exception):
    """Our own mail refused or failed a send (it never raises; it answers None)."""


_SEND_ERRORS = (PodiumAPIError, PodiumNotConnected, _NotSent, mail.EmailNotSent)


@dataclass(frozen=True)
class _Click:
    """What a `send_now` caller hands the sender for this one send."""

    to: list[Recipient] | None = None
    extra_ctx: dict = field(default_factory=dict)
    blocks: dict[str, Block] = field(default_factory=dict)
    channels: frozenset[str] | None = None
    # The masthead logo, attached inline: only where today's email has it (the quote, the
    # affiliate offer). A caller flag, not a list of triggers: Admins add rows on any trigger.
    logo: bool = False
    # The email's hidden inbox preview line (`rendering.preheader`).
    preheader: str = ""


@dataclass
class SendResult:
    """A `send_now`: whether it reached anyone, its Deliveries, and why not if it didn't
    (each failed Delivery's error, else the TouchPoint's reason)."""

    sent: bool
    deliveries: list[Delivery]
    errors: list[str]


_SELECT = (
    "lead",
    "lead__contact",
    "lead__contact__company__billing_contact",
    "lead__billing_contact",
    "lead__assigned_agent",
    "lead__payment",
    "reservation",
    "reservation__lead__contact",
    "reservation__vehicle",
    "reservation__service_type",
)
# Long enough to cover one TouchPoint's sends; a row whose sender died is free again after.
LEASE = timedelta(minutes=5)

_PREFETCH = (
    "reservation__named_passengers",
    "reservation__assignments__vendor",
    "reservation__assignments__driver",
    "reservation__assignments__vehicle",
    "lead__people__contact__planner_account",
    "lead__reservations__stops",
    "lead__reservations__vehicle",
    "lead__reservations__service_type",
)
# What a combining row reads off its same-day siblings: their key, recipients and sheet.
_SIBLING_SELECT = (
    "lead__contact__company__billing_contact",
    "lead__billing_contact",
    "reservation__lead",
    "reservation__vehicle",
    "reservation__service_type",
)
_SIBLING_PREFETCH = (
    "reservation__named_passengers",
    "reservation__assignments__vendor",
    "lead__people__contact__planner_account",
)


def _stops(lookup: str):
    from apps.reservations.services import sheet_stops

    return sheet_stops(lookup)


# --- the row ---------------------------------------------------------------------------


class Rows(dict):
    """A batch's message rows by key, with an index by pk built once for the batch."""

    def __init__(self, rows: Iterable[MessageTemplate]):
        rows = list(rows)
        super().__init__((row.key, row) for row in rows)
        self.by_pk = {row.pk: row for row in rows}


def _row_for(tp: TouchPoint, rows: dict[str, MessageTemplate] | None) -> MessageTemplate | None:
    """The message row a TouchPoint sends, pointing a TouchPoint the old scheduler queued
    (no message yet) at the row for its kind.

    The old scheduler queued one `thank_you` for every finished order and picked the wedding
    wording at send time; such a TouchPoint on a wedding order is pointed at the wedding
    row while that row is on, on its own trigger and matches the order (migration 0024 did
    the same for those already pointing at the plain row). Its kind stays `thank_you`,
    which `events.emit` keys "already thanked" on. The pointer is set in
    memory only; `deliver` saves it once the TouchPoint actually sends, so a later run
    resolves it again."""
    if tp.message_id is not None:
        if rows is None:
            return tp.message
        # The batch's rows, service types already loaded: no join, no query per TouchPoint.
        index = rows.by_pk if isinstance(rows, Rows) else {r.pk: r for r in rows.values()}
        row = index.get(tp.message_id)
        if row is None:
            return tp.message
        tp.message = row
        return row

    def lookup(key: str) -> MessageTemplate | None:
        if rows is not None:
            return rows.get(key)
        return MessageTemplate.objects.prefetch_related("service_types").filter(key=key).first()

    row = lookup(tp.kind)
    if row is None:
        return None
    if tp.kind == "thank_you":
        wedding = lookup("thank_you_wedding")
        if (
            wedding is not None
            and wedding.enabled
            and wedding.archived_at is None
            and rules.has_built_ins(wedding)
            and rules.matches(wedding, tp.lead)
        ):
            row = wedding
    tp.message = row
    return row


def _mark(tp: TouchPoint, *, status: str, error: str = "") -> None:
    tp.status = status
    tp.error = error[:255]
    tp.save(update_fields=["status", "error", "updated_at"])


# --- context ---------------------------------------------------------------------------


def _uses(row: MessageTemplate, name: str) -> bool:
    texts = []
    if row.send_email:
        texts += [row.subject, row.email_body]
    if row.send_sms:
        texts.append(row.sms_body)
    for text in texts:
        try:
            if name in fields._names(text or ""):
                return True
        except ValueError:  # unbalanced braces: the editor refuses these
            continue
    return False


def _reach(person: Recipient) -> set[str]:
    return {a for a in (person.phone, person.email.strip().lower()) if a}


def _combined_trips(tp: TouchPoint, row: MessageTemplate, key: str, person: Recipient) -> list:
    """Every trip this person gets this message for on the combine key's day, in pickup
    order. A seeded confirmation keeps today's group (`rules.confirm_group`); any other
    combining row looks at its other queued sends that day and keeps the trips whose
    recipients include this person."""
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead

    if rules.has_built_ins(row) and row.key in rules.CONFIRMS:
        return rules.confirm_group(tp)
    res = tp.reservation
    mine = _reach(person)
    trips = [res]
    siblings = (
        TouchPoint.objects.filter(
            message=row,
            status=TouchPoint.Status.SCHEDULED,
            lead__status=Lead.Status.BOOKED,
            reservation__pickup_date=res.pickup_date,
        )
        .exclude(pk=tp.pk)
        .exclude(reservation__trip_status__in=CANCELLED_STATUSES)
        .select_related(*_SIBLING_SELECT)
        .prefetch_related(*_SIBLING_PREFETCH, _stops("reservation__stops"))
    )
    for sibling in siblings:
        if rules.combine_key(sibling, row) != key:
            continue
        theirs = rules.recipients_for(
            sibling, row, recipients.resolve(row, sibling.lead, sibling.reservation)
        )
        if any(mine & _reach(p) for p in theirs):
            trips.append(sibling.reservation)
    return sorted(trips, key=lambda t: (t.pickup_time is not None, t.pickup_time or time.min, t.pk))


def _trip_sheet(tp: TouchPoint, row: MessageTemplate, person: Recipient) -> str | None:
    """The combined trip sheet, or None to keep the trip's own sheet. Per-trip keys (a
    seeded message sent once per trip) never combine."""
    from apps.reservations.services import trip_sheet_text

    key = rules.combine_key(tp, row)
    if not key or ":trip:" in key or not _uses(row, "trip_sheet"):
        return None
    trips = _combined_trips(tp, row, key, person)
    if not trips:
        return None
    return "\n\n".join(trip_sheet_text(t) for t in trips)


def context(
    tp: TouchPoint, row: MessageTemplate, recipient: Recipient, extra: dict | None = None
) -> dict[str, str]:
    """Everything a row's wording can use for this recipient. A customer or planner message
    greets the person who receives it, not the booker; an affiliate's keeps the customer's
    fields. A combining row's trip sheet lists the person's whole day. A send with no order
    (an invite, a report) knows only the company and what its caller hands over in
    ``extra``, which has the last word (a staff click's links are built from its request)."""
    if tp.lead_id is None:
        ctx = {
            "first_name": "there",
            "company_name": settings.COMPANY_NAME,
            "company_phone": settings.COMPANY_PHONE,
            "company_email": settings.COMPANY_EMAIL,
        }
    else:
        ctx = build_context(tp.lead, tp.reservation)
    if row.audience in (_A.CUSTOMER, _A.PLANNER):
        ctx["first_name"] = (recipient.name or "").split(" ")[0] or "there"
    if tp.lead_id is not None:
        sheet = _trip_sheet(tp, row, recipient)
        if sheet is not None:
            ctx["trip_sheet"] = sheet
        rules.fill_links(tp, row, ctx, settings.PUBLIC_BASE_URL or "")
    ctx.update(extra or {})
    return ctx


# --- transport -------------------------------------------------------------------------


def _via_postmark(row: MessageTemplate) -> bool:
    trigger = triggers.CATALOG.get(row.trigger)
    return row.audience in _POSTMARK_AUDIENCES or (
        trigger is not None and trigger.email_via == "postmark"
    )


def _address(person: Recipient, channel: str) -> str:
    """The address as it is sent: E.164 for a text, the email as stored."""
    return person.phone if channel == "sms" else person.email.strip()


def _podium_text(row: MessageTemplate, channel: str, texts: dict[str, str]) -> str:
    """The body a Podium send carries: the text, or the email as its subject line, a blank
    line and the body (Podium email has no subject of its own)."""
    if channel == "sms":
        return render(row.sms_body, texts)
    subject = render(row.subject, texts) if row.subject else ""
    body = render(row.email_body, texts) if row.email_body else ""
    return f"{subject}\n\n{body}" if subject else body


def _uid(resp) -> str:
    """Podium's message id from its answer; "" for an answer that carries none."""
    if not isinstance(resp, dict):
        return ""
    return resp.get("uid") or (resp.get("data") or {}).get("uid") or ""


def _call(send, *, tolerant: bool, **kwargs):
    """Call a provider. In a click (``tolerant``) any failure of the call itself is that
    delivery's error, as the button sends always reported it, and the click carries on with
    its other sends; the batch keeps treating an unknown error as unexpected."""
    if not tolerant:
        return send(**kwargs)
    try:
        return send(**kwargs)
    except _SEND_ERRORS:
        raise
    except Exception as exc:  # noqa: BLE001 - recorded on the Delivery
        raise _NotSent(str(exc) or type(exc).__name__) from exc


def _transport(
    row: MessageTemplate,
    channel: str,
    address: str,
    ctx: dict,
    blocks: dict[str, Block] | None = None,
    *,
    logo: bool = False,
    tolerant: bool = False,
    preheader: str = "",
) -> str:
    """Send one rendered message; returns the provider's id. Raises a `_SEND_ERRORS`.

    Our own mail goes out in the branded layout, the blocks' HTML in place
    (`rendering`), with the inline logo only when ``logo`` asks for it; texts and Podium
    email carry the blocks' plain text. ``tolerant``: see `_call`."""
    blocks = blocks or {}
    texts = rendering.texts(ctx, blocks)
    if channel == "email" and _via_postmark(row):
        logo = rendering.logo_path() if logo else None
        subject, layout = rendering.layout_context(
            row, ctx, blocks, logo_cid="logo" if logo else "", preheader=preheader
        )
        provider_id = _call(
            mail.deliver_html_email,
            tolerant=tolerant,
            to=address,
            subject=subject,
            template="message",
            context=layout,
            inline_images={"logo": logo} if logo else None,
            internal=row.audience == _A.STAFF,
            raise_errors=True,
        )
        if provider_id is None:
            raise _NotSent("Email not sent")
        return provider_id
    body = _podium_text(row, channel, texts)
    resp = _call(
        podium.send_message,
        tolerant=tolerant,
        identifier=address,
        channel_type=PODIUM_CHANNEL[channel],
        body=body,
    )
    return _uid(resp)


# --- sending ---------------------------------------------------------------------------


def _record(tp, row, channel, person, address, key, status, reason="", provider_id=""):
    return Delivery.objects.create(
        touchpoint=tp,
        message=row,
        channel=channel,
        recipient_label=person.label[:160],
        address=address,
        combine_key=key,
        status=status,
        reason=reason[:255],
        provider_id=(provider_id or "")[:64],
    )


def _deliveries(tp: TouchPoint, row: MessageTemplate, key: str) -> tuple[list[Delivery], set[str]]:
    """This TouchPoint's Deliveries as the database has them now (never a batch's prefetch:
    they are what makes a re-run send only what is missing), and the addresses (lowercased)
    a send of this row reached for this combine key on another TouchPoint. One that was
    cancelled since (a pickup moved, so a fresh notice was queued) no longer stands in for
    anything. One query."""
    from django.db.models import Q

    wanted = Q(touchpoint=tp)
    if key:
        wanted |= Q(message=row, combine_key=key, status=Delivery.Status.SENT)
    mine, elsewhere = [], set()
    for d in Delivery.objects.filter(wanted).select_related("touchpoint"):
        if d.touchpoint_id == tp.pk:
            mine.append(d)
        elif d.touchpoint.status != TouchPoint.Status.CANCELLED:
            elsewhere.add(d.address.lower())
    return mine, elsewhere


def _old_engine_covered(tp: TouchPoint, row: MessageTemplate) -> bool:
    """For a seeded confirmation: the old engine already sent this wave for the day.

    Its sends left no Deliveries, so at the cut-over its rule (the old `_already_covered`)
    still decides for them: a SENT TouchPoint of the same kind on a trip
    in the same confirm group covers everyone. It ages out with the last of those sends."""
    if not (rules.has_built_ins(row) and row.key in rules.CONFIRMS):
        return False
    group = [t.pk for t in rules.confirm_group(tp)]
    return (
        TouchPoint.objects.filter(
            kind=row.key,
            status=TouchPoint.Status.SENT,
            reservation__in=group,
            deliveries__isnull=True,
        )
        .exclude(pk=tp.pk)
        .exists()
    )


def _day_label(tp: TouchPoint) -> str:
    day = tp.reservation.pickup_date
    return f"{day:%b} {day.day}"


def _finish(
    tp: TouchPoint,
    row: MessageTemplate,
    *,
    sent_now: list[Delivery],
    sent_before: bool,
    errors: list[str],
    covered: int,
) -> bool:
    """Set the TouchPoint from its deliveries: SENT once anything reached anyone, FAILED
    when every attempt failed, otherwise skipped (covered by an earlier send, or nobody had
    an address). Returns whether this call sent anything."""
    if sent_now or sent_before:
        fields_ = ["status", "error", "updated_at"]
        tp.status = TouchPoint.Status.SENT
        tp.error = ""
        if sent_now:
            tp.sent_at = timezone.now()
            fields_.append("sent_at")
            if not tp.podium_message_uid:
                tp.podium_message_uid = next((d.provider_id for d in sent_now), "")
                fields_.append("podium_message_uid")
            if row.trigger in _DRIVER_TRIGGERS and tp.reservation is not None:
                assignment = rules.confirmed_assignment(tp.reservation)
                if assignment is not None:
                    tp.driver_details = assignment.driver_info
                    fields_.append("driver_details")
        tp.save(update_fields=fields_)
        return bool(sent_now)
    if errors:
        _mark(tp, status=TouchPoint.Status.FAILED, error="; ".join(errors))
    elif covered:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error=rules.COVERED)
    else:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error=NO_CHANNEL)
    return False


def _send(
    tp: TouchPoint, row: MessageTemplate, people: list[Recipient], click: _Click | None = None
) -> bool:
    key = rules.combine_key(tp, row)
    done, elsewhere = _deliveries(tp, row, key)
    sent_here = {(d.channel, d.address.lower()) for d in done if d.status == Delivery.Status.SENT}
    reported = {(d.channel, d.recipient_label) for d in done if d.status == Delivery.Status.SKIPPED}
    everyone_covered = _old_engine_covered(tp, row)
    plan: list[tuple[str, Recipient, str]] = []
    covered = 0
    channels = row.channels
    if click is not None and click.channels is not None:
        channels = tuple(c for c in channels if c in click.channels)
    for channel in channels:
        for person in people:
            if channel in person.channels and not _address(person, channel):
                if (channel, person.label[:160]) not in reported:
                    reported.add((channel, person.label[:160]))
                    _record(tp, row, channel, person, "", key, "skipped", MISSING[channel])
        for person, normalised in recipients.addresses(people, channel):
            address = _address(person, channel)
            if (channel, normalised) in sent_here:
                continue
            if everyone_covered or normalised in elsewhere:
                covered += 1
                reason = f"Already sent for {_day_label(tp)}"
                _record(tp, row, channel, person, address, key, "skipped", reason)
                continue
            plan.append((channel, person, address))

    # Every wording is rendered before the first send, so a bad one stops the TouchPoint
    # before anyone has heard from it.
    contexts: dict[Recipient, dict] = {}
    extra = click.extra_ctx if click is not None else None
    blocks = click.blocks if click is not None else None
    logo = click.logo if click is not None else False
    tolerant = click is not None
    preheader = click.preheader if click is not None else ""
    for _, person, _ in plan:
        if person not in contexts:
            contexts[person] = context(tp, row, person, extra)
    sent_now: list[Delivery] = []
    errors: list[str] = []
    for channel, person, address in plan:
        try:
            provider_id = _transport(
                row,
                channel,
                address,
                contexts[person],
                blocks,
                logo=logo,
                tolerant=tolerant,
                preheader=preheader,
            )
        except _SEND_ERRORS as exc:
            errors.append(f"{channel}: {exc}")
            _record(tp, row, channel, person, address, key, "failed", str(exc))
        else:
            sent_now.append(
                _record(tp, row, channel, person, address, key, "sent", "", provider_id)
            )
    return _finish(
        tp, row, sent_now=sent_now, sent_before=bool(sent_here), errors=errors, covered=covered
    )


def _review(tp: TouchPoint, row: MessageTemplate, people: list[Recipient]) -> bool:
    """The seeded review request: Podium's review invite and a `Review` row per phone, then
    the text carrying the invite's link (as the old `_send_review_invite`)."""
    done, _ = _deliveries(tp, row, "")
    sent_here = {d.address for d in done if d.status == Delivery.Status.SENT and d.channel == "sms"}
    sent_now: list[Delivery] = []
    errors: list[str] = []
    for person, phone in recipients.addresses(people, "sms"):
        if phone in sent_here:
            continue
        first, last = _split_name(person.name)
        try:
            invite = podium.create_review_invitation(phone=phone, first_name=first, last_name=last)
        except (PodiumAPIError, PodiumNotConnected) as exc:
            errors.append(str(exc))
            _record(tp, row, "sms", person, phone, "", "failed", str(exc))
            continue
        Review.objects.create(
            lead=tp.lead,
            contact_id=person.contact_id,
            podium_review_invite_uid=invite.get("uid", ""),
            delivery_status=Review.DeliveryStatus.SENT,
            requested_at=timezone.now(),
        )
        ctx = context(tp, row, person)
        ctx["review_link"] = invite.get("url") or invite.get("link") or ""
        try:
            provider_id = _transport(row, "sms", phone, ctx)
        except (PodiumAPIError, PodiumNotConnected) as exc:
            errors.append(str(exc))
            _record(tp, row, "sms", person, phone, "", "failed", str(exc))
        else:
            sent_now.append(_record(tp, row, "sms", person, phone, "", "sent", "", provider_id))
    return _finish(
        tp, row, sent_now=sent_now, sent_before=bool(sent_here), errors=errors, covered=0
    )


def _claim(tp: TouchPoint) -> bool:
    """Take the TouchPoint for this send, or learn someone else has it.

    One conditional update, so it holds on MySQL and Postgres alike: it succeeds only while
    the row is still SCHEDULED, still due, and unchanged since this snapshot was read, and it
    leases the row by moving `updated_at` a few minutes ahead. A stale snapshot (the cron's
    batch after an inline send, or the reverse) fails the match; a fresh read during someone
    else's send sees the lease. A row re-dated in bulk (`events.redate`, which leaves
    `updated_at` alone) since it was read is no longer due. Every way out of `deliver` saves
    `updated_at` again, which ends the lease; a sender that died leaves it to expire."""
    now = timezone.now()
    snapshot = tp.updated_at
    if tp.status == TouchPoint.Status.SCHEDULED and snapshot is not None and snapshot <= now:
        lease = now + LEASE
        won = TouchPoint.objects.filter(
            pk=tp.pk,
            status=TouchPoint.Status.SCHEDULED,
            updated_at=snapshot,
            scheduled_for__lte=now,
        ).update(updated_at=lease)
        if won:
            tp.updated_at = lease
            return True
    tp.refresh_from_db()
    return False


def deliver(
    tp: TouchPoint,
    *,
    master: bool | None = None,
    rows: dict[str, MessageTemplate] | None = None,
) -> bool:
    """Send one SCHEDULED TouchPoint now if its checks allow; True iff a Delivery was sent
    by this call. A TouchPoint another call is sending, or that changed since `tp` was read,
    is left to that call.

    A wait leaves it SCHEDULED with the reason in `error`; a skip or cancel closes it with
    the reason. Addresses this TouchPoint already reached are never sent again, so a re-run
    only sends what is missing. ``master`` and ``rows`` let a batch read the master switch and
    the message rows once."""
    if not _claim(tp):
        return False
    return _claimed(tp, master=master, rows=rows)


def _claimed(tp: TouchPoint, *, master, rows, click: _Click | None = None) -> bool:
    tp.send_cache = {}  # read by `rules.confirm_group`, for this send only
    try:
        return _deliver(tp, master=master, rows=rows, click=click)
    finally:
        tp.__dict__.pop("send_cache", None)


def _deliver(tp: TouchPoint, *, master, rows, click: _Click | None = None) -> bool:
    pointed = tp.message_id is None
    row = _row_for(tp, rows)
    if row is None:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error=rules.DISABLED)
        return False
    trigger = triggers.CATALOG.get(row.trigger)
    sent_now = trigger is not None and (trigger.kind in _SENT_NOW or trigger.caller_sends)
    if click is None and sent_now:
        # Only `send_now` sends these, with what its caller handed over; one left behind
        # by an interrupted click is settled rather than sent without it: SENT if it had
        # reached anyone, else closed. Only the cron gets here, so with TOUCHPOINTS_ENABLED
        # off such a row simply stays SCHEDULED.
        _settle_after_error(tp, status=TouchPoint.Status.SKIPPED, error=INTERRUPTED)
        return False
    verdict, reason = rules.check(tp, row, master=master)
    if verdict == "wait" and click is not None:
        verdict = "skip"  # a click can't wait: it sends now or says why not
    if verdict == "wait":
        tp.error = reason[:255]
        tp.save(update_fields=["error", "updated_at"])  # also ends the lease
        return False
    if verdict != "send":
        status = TouchPoint.Status.CANCELLED if verdict == "cancel" else TouchPoint.Status.SKIPPED
        _mark(tp, status=status, error=reason)
        return False
    if pointed:
        TouchPoint.objects.filter(pk=tp.pk).update(message=row)
    if click is not None and click.to is not None:
        people = list(click.to)
    elif tp.lead_id is None and row.audience != _A.STAFF:
        people = []  # nobody to resolve from without an order
    else:
        people = rules.recipients_for(tp, row, recipients.resolve(row, tp.lead, tp.reservation))
    if not people:
        _mark(tp, status=TouchPoint.Status.SKIPPED, error=NO_RECIPIENT)
        return False
    if row.key == "review_request" and rules.has_built_ins(row):
        return _review(tp, row, people)
    return _send(tp, row, people, click)


def _settle_after_error(
    tp: TouchPoint, *, status: str = TouchPoint.Status.FAILED, error: str = "unexpected error"
) -> None:
    """A send that blew up part-way: SENT if anyone was reached, else today's FAILED (or the
    given status and reason)."""
    if Delivery.objects.filter(touchpoint=tp, status=Delivery.Status.SENT).exists():
        tp.status = TouchPoint.Status.SENT
        tp.error = ""
        tp.sent_at = tp.sent_at or timezone.now()
        tp.save(update_fields=["status", "error", "sent_at", "updated_at"])
    else:
        _mark(tp, status=status, error=error)


# --- batches ---------------------------------------------------------------------------


def _due():
    return TouchPoint.objects.filter(
        status=TouchPoint.Status.SCHEDULED, scheduled_for__lte=timezone.now()
    )


def _send_batch(due) -> int:
    """Send a batch of due rows; returns the count sent.

    With the go-live switch off (APC-90), each due row is closed as SKIPPED with the reason
    rather than left SCHEDULED: left alone they would pile up and all send at once, weeks
    stale, the moment the switch flips. A row someone holds the lease on (a click sending it
    right now) is left to them. One bad row never stops the rest."""
    if not outbound.enabled():
        held = due.filter(updated_at__lte=timezone.now()).update(
            status=TouchPoint.Status.SKIPPED, error=OUTBOUND_OFF, updated_at=timezone.now()
        )
        if held:
            logger.info("outbound off: %d due touch-point(s) skipped, not sent", held)
        return 0

    master = NotificationConfig.load().enabled
    rows = Rows(MessageTemplate.objects.prefetch_related("service_types"))
    sent = 0
    batch = (
        due.select_related(*_SELECT)
        .prefetch_related(*_PREFETCH, _stops("reservation__stops"))
        .order_by("scheduled_for", "pk")
    )
    for tp in batch:
        try:
            if deliver(tp, master=master, rows=rows):
                sent += 1
        except Exception:  # noqa: BLE001 - one bad row must not kill the run
            logger.exception("touch-point %s failed to send", tp.pk)
            _settle_after_error(tp)
    return sent


def run_touchpoints() -> int:
    """The `run-touchpoints` cron job: send every due SCHEDULED TouchPoint. Returns the
    count sent. Does nothing while `TOUCHPOINTS_ENABLED` is off."""
    if not settings.TOUCHPOINTS_ENABLED:
        return 0
    return _send_batch(_due())


def send_due(touchpoint_ids: Iterable[int]) -> None:
    """Send the given TouchPoints that are due now, as the cron would (an event's sends,
    after its transaction commits). Not-yet-due rows wait for the cron. Never raises: it
    runs after someone else's save, and whatever is left unsent the cron picks up."""
    try:
        ids = list(touchpoint_ids)
        if not ids or not settings.TOUCHPOINTS_ENABLED:
            return
        _send_batch(_due().filter(pk__in=ids))
    except Exception:  # noqa: BLE001 - an inline send must never break the caller
        logger.exception("inline send of touch-points %s failed", touchpoint_ids)


# --- staff clicks, invites and reports -------------------------------------------------


def send_now(
    key: str,
    lead=None,
    reservation=None,
    *,
    to: list[Recipient] | None = None,
    extra_ctx: dict | None = None,
    blocks: dict[str, Block] | None = None,
    channels: set[str] | None = None,
    logo: bool = False,
    preheader: str = "",
) -> SendResult:
    """Send the row ``key`` now, inside the click: a staff-click send, an invite or password
    reset, or a staff report (spec C sections 3 and 8). Never raises for a send.

    It leaves the same trail as every other send: a TouchPoint (no ``lead`` for an invite or
    a report) and a Delivery per person per channel, through the same deliver path. ``to``
    replaces the row's recipients; ``extra_ctx`` adds or overrides placeholder values (the
    caller's links, built from its request); ``blocks`` fills the generated parts;
    ``channels`` narrows the row's channels to the ones the user picked; ``logo`` attaches
    the masthead logo (only the quote and the affiliate offer carry it today); ``preheader``
    is the email's inbox preview line.

    Gated as today's button sends are: `OUTBOUND_ENABLED` refuses anything but staff mail,
    which goes as `internal`; the row's own switch applies; `TOUCHPOINTS_ENABLED` and the
    master switch do not (they govern the messages nobody clicked). An event row its service
    sends itself (`Trigger.caller_sends`, the wedding-details email) is sent the same way.
    """
    row = MessageTemplate.objects.prefetch_related("service_types").get(key=key)
    click = _Click(
        to=None if to is None else list(to),
        extra_ctx=dict(extra_ctx or {}),
        blocks=dict(blocks or {}),
        channels=None if channels is None else frozenset(channels),
        logo=logo,
        preheader=preheader,
    )
    now = timezone.now()
    with transaction.atomic():
        tp = TouchPoint.objects.create(
            lead=lead,
            reservation=reservation,
            message=row,
            kind=row.key,
            status=TouchPoint.Status.SCHEDULED,
            scheduled_for=now,
        )
        # Leased from birth (see `_claim`): the cron's batch, which takes any due SCHEDULED
        # row, never sends this one without the click's recipients, values and blocks.
        tp.updated_at = now + LEASE
        TouchPoint.objects.filter(pk=tp.pk).update(updated_at=tp.updated_at)

    if row.audience != _A.STAFF and not outbound.enabled():
        logger.info("outbound off: %s not sent", row.key)
        _mark(tp, status=TouchPoint.Status.SKIPPED, error=OUTBOUND_OFF)
        return SendResult(sent=False, deliveries=[], errors=[outbound.OFF_MESSAGE])

    try:
        sent = _claimed(tp, master=True, rows=None, click=click)
    except Exception:  # noqa: BLE001 - a send must never break the click
        logger.exception("send of %s (touch-point %s) failed", row.key, tp.pk)
        _settle_after_error(tp)
        sent = tp.status == TouchPoint.Status.SENT
    deliveries = list(Delivery.objects.filter(touchpoint=tp).order_by("pk"))
    errors = [d.reason for d in deliveries if d.status == Delivery.Status.FAILED]
    if not sent and not errors and tp.error:
        errors = [tp.error]
    return SendResult(sent=sent, deliveries=deliveries, errors=errors)


def podium_text(
    key: str,
    lead,
    recipient: Recipient,
    channel: str,
    *,
    reservation=None,
    extra_ctx: dict | None = None,
) -> str:
    """What a click's Podium send of row ``key`` to ``recipient`` on ``channel`` carries,
    rendered as `send_now` renders it: for a caller that records the message it sent on the
    customer's conversation."""
    row = MessageTemplate.objects.get(key=key)
    tp = TouchPoint(lead=lead, reservation=reservation, message=row, kind=row.key)
    return _podium_text(row, channel, context(tp, row, recipient, extra_ctx))
