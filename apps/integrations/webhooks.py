"""Inbound Podium and Calendly webhook processing.

The exact event envelope is mapped from Podium's documented message fields; the
raw payload is always stored on PodiumEvent so we can refine against real traffic.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.leads.models import Lead
from apps.messaging import services as messaging_services
from apps.messaging.models import Conversation, Message

from . import podium
from .calendly import parse_start_time
from .models import CalendlyEvent, PodiumEvent

logger = logging.getLogger(__name__)

CHANNEL_MAP = {
    "phone": Message.Channel.SMS,
    "sms": Message.Channel.SMS,
    "email": Message.Channel.EMAIL,
    "facebook": Message.Channel.FACEBOOK,
    "whatsapp": Message.Channel.WHATSAPP,
    "apple": Message.Channel.APPLE,
}


def podium_event_type(payload: dict) -> str:
    """Extract the event type from a Podium webhook payload.

    Podium nests it under `metadata.eventType`. The top-level lookups are kept as a
    fallback for hand-built/replayed payloads. Returns "" when absent — callers must
    NOT assume a message event, or an unrecognised payload becomes a fake customer
    message (see `process_podium_webhook`).
    """
    metadata = payload.get("metadata") or {}
    return metadata.get("eventType") or payload.get("eventType") or payload.get("type") or ""


def podium_event_uid(payload: dict) -> str | None:
    """Podium's stable id for the event itself, constant across delivery retries.

    Returns None (not "") when absent so the unique index treats unidentifiable payloads
    as distinct rather than colliding them all onto one key.
    """
    metadata = payload.get("metadata") or {}
    return metadata.get("eventUid") or None


def _event_for_uid(event_uid: str | None) -> PodiumEvent | None:
    if not event_uid:
        return None
    return PodiumEvent.objects.filter(event_uid=event_uid).first()


def process_podium_webhook(payload: dict) -> PodiumEvent:
    event_type = podium_event_type(payload)
    data = payload.get("data", payload)

    # Retries are no-ops. Podium re-delivers when it doesn't see a timely 200 — on
    # 2026-07-31 one message.failed arrived 8 times — and reprocessing would re-run the
    # side effects and pile up duplicate rows.
    event_uid = podium_event_uid(payload)
    seen = _event_for_uid(event_uid)
    if seen is not None:
        return seen

    try:
        with transaction.atomic():
            event = PodiumEvent.objects.create(
                event_type=event_type, payload=payload, event_uid=event_uid
            )
    except IntegrityError:
        # Lost the insert race against a concurrent retry — the winner already has it.
        won = _event_for_uid(event_uid)
        if won is None:
            raise
        return won

    if event_type == PodiumEvent.EventType.MESSAGE_RECEIVED:
        event.conversation = _ingest_inbound(data)
    elif event_type == PodiumEvent.EventType.MESSAGE_SENT:
        event.conversation = _ingest_outbound(data)
    elif event_type == PodiumEvent.EventType.MESSAGE_FAILED:
        _mark_failed(data)
    else:
        # Left unprocessed on purpose: unhandled events stay queryable rather than
        # being silently absorbed as inbound messages.
        logger.warning("podium webhook: unhandled eventType %r, skipping", event_type)
        event.save(update_fields=["updated_at"])
        return event

    event.processed = True
    event.save(update_fields=["conversation", "processed", "updated_at"])
    return event


def _ingest_inbound(data: dict) -> Conversation | None:
    msg_uid = data.get("uid", "")
    existing = Message.objects.filter(podium_message_uid=msg_uid).first() if msg_uid else None
    if existing:
        return existing.conversation  # dedupe — Podium may retry

    contact_data = data.get("contact") or {}
    conversation_data = data.get("conversation") or {}
    channel = conversation_data.get("channel") or data.get("channel") or {}
    identifier = channel.get("identifier") or contact_data.get("phoneNumber") or ""
    channel_type = channel.get("type", "phone")

    conversation = _resolve_conversation(contact_data, identifier)
    messaging_services.record_inbound(
        conversation,
        channel=CHANNEL_MAP.get(channel_type, Message.Channel.SMS),
        body=data.get("body", ""),
        podium_message_uid=msg_uid,
        podium_conversation_uid=conversation_data.get("uid", ""),
        sender_name=(contact_data.get("name") or data.get("contactName") or "").strip(),
    )
    return conversation


def _contact_by_phone(identifier: str) -> Contact | None:
    """Match a Podium identifier (E.164) against stored phones in either format.

    Both `_resolve_lead` and `_resolve_lead_readonly` match on phone. Sharing one
    helper keeps them from drifting — they have already been duplicated once.
    """
    if not identifier:
        return None
    normalized = to_e164(identifier)
    lookup = Q(phone=identifier)
    if normalized and normalized != identifier:
        lookup |= Q(phone=normalized)
    return Contact.objects.filter(lookup).first()


def _resolve_conversation(contact_data: dict, identifier: str) -> Conversation:
    """Match (or create) the Contact behind a Podium message, then its conversation.

    Deliberately does NOT create a Lead: the Podium account is the main business
    number, so inbound traffic includes wrong numbers, vendors and spam alongside real
    inquiries. Qualifying is an explicit agent action (the inbox "Create lead" button).
    """
    uid = contact_data.get("uid", "")
    contact = None
    if uid:
        contact = Contact.objects.filter(podium_contact_uid=uid).first()
    if contact is None:
        contact = _contact_by_phone(identifier)
    if contact is None:
        contact = Contact.objects.create(
            name=contact_data.get("name") or identifier or "Podium contact",
            phone=to_e164(identifier) or identifier or "",
            podium_contact_uid=uid,
            channel=Channel.PHONE,
        )
    elif uid and not contact.podium_contact_uid:
        contact.podium_contact_uid = uid
        contact.save(update_fields=["podium_contact_uid"])

    return messaging_services.conversation_for(contact)


def _ingest_outbound(data: dict) -> Conversation | None:
    """Mirror an outbound Podium send into the inbox thread.

    If the send came from our own composer a Message with this uid already exists —
    just confirm its delivery status. Otherwise (sent from the Podium web app or
    another integration) record it, creating the conversation if the number is new:
    agent-initiated outreach belongs in the inbox too. This used to log-and-skip to
    avoid orphaning a contact on a lead; conversations are decoupled from leads now,
    so there is nothing to orphan.
    """
    msg_uid = data.get("uid", "")
    existing = Message.objects.filter(podium_message_uid=msg_uid).first() if msg_uid else None
    if existing:
        update_fields = []
        if existing.delivery_status != Message.DeliveryStatus.SENT:
            existing.delivery_status = Message.DeliveryStatus.SENT
            update_fields.append("delivery_status")
        if existing.sent_at is None:
            existing.sent_at = timezone.now()
            update_fields.append("sent_at")
        if update_fields:
            existing.save(update_fields=update_fields)
        return existing.conversation

    contact_data = data.get("contact") or {}
    conversation_data = data.get("conversation") or {}
    channel = conversation_data.get("channel") or data.get("channel") or {}
    identifier = channel.get("identifier") or contact_data.get("phoneNumber") or ""
    channel_type = channel.get("type", "phone")

    conversation = _resolve_conversation(contact_data, identifier)
    sender_uid = data.get("senderUid") or (data.get("sender") or {}).get("uid") or ""
    messaging_services.record_outbound(
        conversation,
        channel=CHANNEL_MAP.get(channel_type, Message.Channel.SMS),
        body=data.get("body", ""),
        podium_message_uid=msg_uid,
        podium_conversation_uid=conversation_data.get("uid", ""),
        podium_sender_uid=sender_uid,
        sender_name=podium.user_name_map().get(sender_uid, "") if sender_uid else "",
    )
    return conversation


def _mark_failed(data: dict) -> None:
    uid = data.get("uid", "")
    if not uid:
        return
    Message.objects.filter(podium_message_uid=uid).update(
        delivery_status=Message.DeliveryStatus.FAILED,
        failure_reason=data.get("failureReason", ""),
    )


# --- Calendly ------------------------------------------------------------------

# A Calendly booking carries no TRIP date — only the call time — so there is nothing
# to compare against a lead's pickup dates. Lead state is the only signal available:
# exactly one recently-touched open lead means the caller is almost certainly ringing
# about that quote. Two is ambiguous, and guessing wrong buries the note on the wrong
# quote, so ambiguity creates a new lead instead.
OPEN_LEAD_STATUSES = (Lead.Status.NEW, Lead.Status.QUOTED)
LEAD_ATTACH_WINDOW = timedelta(days=90)


def calendly_phone(data: dict) -> str:
    """The invitee's phone, if they gave one anywhere.

    Calendly puts a text-reminder number on `text_reminder_number` and everything
    else in the free-form `questions_and_answers`. Neither is guaranteed: a booking
    with an email and nothing more is normal, which `Contact.match_or_create`
    handles — it matches on either identifier.
    """
    direct = (data.get("text_reminder_number") or "").strip()
    if direct:
        return direct
    for qa in data.get("questions_and_answers") or []:
        if "phone" in (qa.get("question") or "").lower():
            return (qa.get("answer") or "").strip()
    return ""


def _calendly_attribution(data: dict) -> str:
    """ "Source: google / cpc / spring-charter", or "" when nothing was tracked.

    utm_* only ever exists in this payload — nowhere a person would otherwise look —
    so which ad produced the call is lost unless it is written down here.
    """
    tracking = data.get("tracking") or {}
    parts = [
        (tracking.get(key) or "").strip() for key in ("utm_source", "utm_medium", "utm_campaign")
    ]
    return f"Source: {' / '.join(p for p in parts if p)}" if any(parts) else ""


def _calendly_note(data: dict, *, moved: bool = False) -> str:
    """The agent-facing line on the Lead, in the company timezone.

    Calendly sends UTC; the office reads the pipeline in ET, so this renders in
    TIME_ZONE with the abbreviation rather than dropping a raw UTC string in notes.
    cancel_url/reschedule_url are deliberately NOT here — they are long, they would
    dominate a pipeline card, and intake_payload keeps them recoverable.
    """
    scheduled = data.get("scheduled_event") or {}
    name = (scheduled.get("name") or "Call").strip()
    start = parse_start_time(scheduled.get("start_time") or "")
    when = f" — {timezone.localtime(start):%a %b %-d, %Y at %-I:%M %p %Z}" if start else ""
    headline = f"Calendly call moved{when}." if moved else f"{name} booked via Calendly{when}."
    return "\n".join(line for line in (headline, _calendly_attribution(data)) if line)


def _append_note(lead: Lead, line: str) -> None:
    lead.notes = f"{lead.notes}\n{line}".strip()
    lead.save(update_fields=["notes", "updated_at"])


def _lead_for_invitee(uri: str) -> Lead | None:
    """The Lead created for a given invitee URI, if we recorded one."""
    if not uri:
        return None
    created = CalendlyEvent.objects.filter(
        idempotency_key=f"{CalendlyEvent.EventType.INVITEE_CREATED}:{uri}"
    ).first()
    return created.lead if created else None


def _open_lead_for(contact) -> Lead | None:
    """The one open lead this booking should attach to, or None to create a new one."""
    open_leads = list(
        contact.leads.filter(
            status__in=OPEN_LEAD_STATUSES,
            updated_at__gte=timezone.now() - LEAD_ATTACH_WINDOW,
        )[:2]
    )
    return open_leads[0] if len(open_leads) == 1 else None


def _calendly_lead(data: dict) -> Lead:
    """Contact + Lead for a booked discovery call.

    A reschedule arrives as a NEW invitee carrying `old_invitee`; when that points at
    a lead we already have, this updates it rather than creating a duplicate. Failing
    to correlate is never fatal — a real booking is never dropped.

    Deliberately no touch-points: TP1/TP2 copy is website-worded and reads badly to
    someone who has just booked a call. `intake_payload` archives the invitee record
    verbatim; `notes` stays the human-readable version an agent may freely edit.
    """
    moved_from = _lead_for_invitee(str(data.get("old_invitee") or ""))
    if moved_from is not None:
        _append_note(moved_from, _calendly_note(data, moved=True))
        return moved_from

    contact = Contact.objects.match_or_create(
        name=(data.get("name") or "").strip() or "Calendly invitee",
        email=(data.get("email") or "").strip(),
        phone=calendly_phone(data),
        channel=Channel.WEBSITE,
    )
    existing = _open_lead_for(contact)
    if existing is not None:
        _append_note(existing, _calendly_note(data))
        return existing

    return Lead.objects.create(
        contact=contact,
        status=Lead.Status.NEW,
        channel=Channel.WEBSITE,
        notes=_calendly_note(data),
        intake_payload=data,
    )


def _calendly_cancellation(data: dict) -> Lead | None:
    """Annotate the Lead behind a canceled call. Never destructive.

    By the time a call is canceled the Lead may be a real quote in the pipeline, so
    an agent decides what a cancellation means — this only records that it happened.

    A RESCHEDULE also fires invitee.canceled, flagged `rescheduled: true`, paired
    with an invitee.created carrying the new time. Writing "canceled" for that would
    be simply false, so the note is left to the paired created event.
    """
    lead = _lead_for_invitee(str(data.get("uri") or ""))
    if lead is None:
        return None
    if data.get("rescheduled") is True:
        logger.info("Calendly webhook: invitee rescheduled, awaiting the new booking.")
        return lead
    _append_note(lead, "Calendly call canceled.")
    return lead


def _calendly_event_type_allowed(data: dict) -> bool:
    """Whether this booking is for the event type we care about.

    The subscription is account-wide, so without this every meeting booked on the
    account — vendor calls, internal 1:1s — would become a sales lead. Only
    invitee.created is filtered: a cancellation correlates by invitee URI and
    already no-ops when no created event was recorded.
    """
    wanted = getattr(settings, "CALENDLY_EVENT_TYPE_URI", "")
    if not wanted:
        return True
    return (data.get("scheduled_event") or {}).get("event_type") == wanted


def process_calendly_webhook(payload: dict) -> CalendlyEvent | None:
    """Record a Calendly webhook and act on it.

    Returns None for anything unhandled, so an unrecognised payload can never fall
    through into lead creation — the same trap `podium_event_type` guards against.
    """
    event_type = str(payload.get("event") or "")
    if event_type not in CalendlyEvent.EventType.values:
        logger.info("Calendly webhook: ignoring event type %r", event_type)
        return None

    data = payload.get("payload") or {}
    invitee_uri = str(data.get("uri") or "")
    if not invitee_uri:
        logger.warning("Calendly webhook: %s with no invitee uri — ignored.", event_type)
        return None

    if event_type == CalendlyEvent.EventType.INVITEE_CREATED and not _calendly_event_type_allowed(
        data
    ):
        logger.info("Calendly webhook: booking for another event type — ignored.")
        return None

    key = f"{event_type}:{invitee_uri}"
    try:
        with transaction.atomic():
            event = CalendlyEvent.objects.create(
                idempotency_key=key, event_type=event_type, payload=payload
            )
    except IntegrityError:
        # Calendly re-delivers until it sees a timely 2xx, and two retries can be in
        # flight at once — so the unique key, not an app-level check, is what stops
        # one booked call becoming two Leads.
        return CalendlyEvent.objects.filter(idempotency_key=key).first()

    if event_type == CalendlyEvent.EventType.INVITEE_CREATED:
        event.lead = _calendly_lead(data)
    else:
        event.lead = _calendly_cancellation(data)
    event.processed = True
    event.save(update_fields=["lead", "processed", "updated_at"])
    return event
