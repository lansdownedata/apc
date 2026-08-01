"""Inbound Podium webhook processing (message.received / sent / failed).

The exact event envelope is mapped from Podium's documented message fields; the
raw payload is always stored on PodiumEvent so we can refine against real traffic.
"""

import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.messaging import services as messaging_services
from apps.messaging.models import Conversation, Message

from . import podium
from .models import PodiumEvent

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
