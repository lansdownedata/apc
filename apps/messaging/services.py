"""Message-write helpers.

Three call sites create Message rows — the inbound Podium webhook, the outbound
webhook mirror, and the inbox composer — and all three must stamp the conversation's
`last_message_at`. Routing them through here keeps that from drifting (the Podium
channel map has already been duplicated and diverged once; see apps/messaging/views.py).
"""

from __future__ import annotations

from django.utils import timezone

from apps.contacts.models import Contact

from .models import Conversation, Message


def conversation_for(contact: Contact) -> Conversation:
    """Get or create the contact's conversation.

    Conversations are created lazily — a contact from the website form or the New Lead
    modal has none until a message is written. Callers must never assume
    `contact.conversation` exists; the reverse OneToOne accessor raises
    `RelatedObjectDoesNotExist` when it doesn't.
    """
    conversation, _ = Conversation.objects.get_or_create(contact=contact)
    return conversation


def _touch(conversation: Conversation, when) -> None:
    """Advance `last_message_at`. Never changes `status` — archiving is permanent."""
    conversation.last_message_at = when
    conversation.save(update_fields=["last_message_at", "updated_at"])


def record_inbound(
    conversation: Conversation,
    *,
    channel: str,
    body: str,
    podium_message_uid: str = "",
    podium_conversation_uid: str = "",
    sender_name: str = "",
) -> Message:
    """Persist a message received from the customer."""
    message = Message.objects.create(
        conversation=conversation,
        direction=Message.Direction.IN,
        channel=channel,
        body=body,
        podium_message_uid=podium_message_uid,
        podium_conversation_uid=podium_conversation_uid,
        sender_name=sender_name,
        delivery_status=Message.DeliveryStatus.RECEIVED,
    )
    _touch(conversation, message.created_at)
    return message


def record_outbound(
    conversation: Conversation,
    *,
    channel: str,
    body: str,
    podium_message_uid: str = "",
    podium_conversation_uid: str = "",
    sender_name: str = "",
    podium_sender_uid: str = "",
) -> Message:
    """Persist a message we sent — from our composer or mirrored from Podium."""
    now = timezone.now()
    message = Message.objects.create(
        conversation=conversation,
        direction=Message.Direction.OUT,
        channel=channel,
        body=body,
        podium_message_uid=podium_message_uid,
        podium_conversation_uid=podium_conversation_uid,
        podium_sender_uid=podium_sender_uid,
        sender_name=sender_name,
        delivery_status=Message.DeliveryStatus.SENT,
        sent_at=now,
    )
    _touch(conversation, now)
    return message
