"""Inbound Podium webhook processing (message.received / sent / failed).

The exact event envelope is mapped from Podium's documented message fields; the
raw payload is always stored on PodiumEvent so we can refine against real traffic.
"""

import logging

from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.models import Message

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


def process_podium_webhook(payload: dict) -> PodiumEvent:
    event_type = payload.get("eventType") or payload.get("type") or "message.received"
    data = payload.get("data", payload)
    event = PodiumEvent.objects.create(event_type=event_type, payload=payload)

    if event_type == PodiumEvent.EventType.MESSAGE_RECEIVED:
        event.lead = _ingest_inbound(data)
    elif event_type == PodiumEvent.EventType.MESSAGE_SENT:
        event.lead = _ingest_outbound(data)
    elif event_type == PodiumEvent.EventType.MESSAGE_FAILED:
        _mark_failed(data)

    event.processed = True
    event.save(update_fields=["lead", "processed", "updated_at"])
    return event


def _ingest_inbound(data: dict) -> Lead:
    msg_uid = data.get("uid", "")
    existing = Message.objects.filter(podium_message_uid=msg_uid).first() if msg_uid else None
    if existing:
        return existing.lead  # dedupe — Podium may retry

    contact_data = data.get("contact") or {}
    conversation = data.get("conversation") or {}
    channel = conversation.get("channel") or data.get("channel") or {}
    identifier = channel.get("identifier") or contact_data.get("phoneNumber") or ""
    channel_type = channel.get("type", "phone")

    lead = _resolve_lead(contact_data, identifier)
    Message.objects.create(
        lead=lead,
        direction=Message.Direction.IN,
        channel=CHANNEL_MAP.get(channel_type, Message.Channel.SMS),
        body=data.get("body", ""),
        podium_conversation_uid=conversation.get("uid", ""),
        podium_message_uid=msg_uid,
        delivery_status=Message.DeliveryStatus.RECEIVED,
    )
    return lead


def _resolve_lead(contact_data: dict, identifier: str) -> Lead:
    uid = contact_data.get("uid", "")
    contact = None
    if uid:
        contact = Contact.objects.filter(podium_contact_uid=uid).first()
    if contact is None and identifier:
        contact = Contact.objects.filter(phone=identifier).first()
    if contact is None:
        contact = Contact.objects.create(
            name=contact_data.get("name") or identifier or "Podium contact",
            phone=identifier or "",
            podium_contact_uid=uid,
            channel=Channel.PHONE,
        )
    elif uid and not contact.podium_contact_uid:
        contact.podium_contact_uid = uid
        contact.save(update_fields=["podium_contact_uid"])

    lead = contact.leads.order_by("-id").first()
    if lead is None:
        lead = Lead.objects.create(contact=contact, channel=Channel.PHONE)
        touchpoints.schedule_lead_created(lead)
    return lead


def _ingest_outbound(data: dict) -> Lead | None:
    """Mirror an outbound Podium send into the inbox thread.

    If the send was made from our own composer, a Message with this uid already
    exists — just update its delivery status. Otherwise (sent from the Podium web
    app / another integration) create the OUT Message on the matching lead. If no
    lead can be matched, log-and-skip rather than creating an orphan contact for an
    outbound message.
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
        return existing.lead

    contact_data = data.get("contact") or {}
    conversation = data.get("conversation") or {}
    channel = conversation.get("channel") or data.get("channel") or {}
    identifier = channel.get("identifier") or contact_data.get("phoneNumber") or ""
    channel_type = channel.get("type", "phone")

    lead = _resolve_lead_readonly(contact_data, identifier)
    if lead is None:
        logger.warning("message.sent uid=%s: no matching contact/lead, skipping", msg_uid)
        return None

    Message.objects.create(
        lead=lead,
        direction=Message.Direction.OUT,
        channel=CHANNEL_MAP.get(channel_type, Message.Channel.SMS),
        body=data.get("body", ""),
        podium_conversation_uid=conversation.get("uid", ""),
        podium_message_uid=msg_uid,
        delivery_status=Message.DeliveryStatus.SENT,
        sent_at=timezone.now(),
    )
    return lead


def _resolve_lead_readonly(contact_data: dict, identifier: str) -> Lead | None:
    """Like `_resolve_lead` but never creates a Contact/Lead — for outbound mirroring."""
    uid = contact_data.get("uid", "")
    contact = None
    if uid:
        contact = Contact.objects.filter(podium_contact_uid=uid).first()
    if contact is None and identifier:
        contact = Contact.objects.filter(phone=identifier).first()
    if contact is None:
        return None
    return contact.leads.order_by("-id").first()


def _mark_failed(data: dict) -> None:
    uid = data.get("uid", "")
    if not uid:
        return
    Message.objects.filter(podium_message_uid=uid).update(
        delivery_status=Message.DeliveryStatus.FAILED,
        failure_reason=data.get("failureReason", ""),
    )
