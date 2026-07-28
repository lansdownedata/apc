import pytest
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.messaging import services
from apps.messaging.factories import ConversationFactory
from apps.messaging.models import Conversation, Message

pytestmark = pytest.mark.django_db


def test_conversation_for_creates_one_then_reuses_it():
    contact = ContactFactory()
    first = services.conversation_for(contact)
    second = services.conversation_for(contact)
    assert first == second
    assert Conversation.objects.count() == 1


def test_record_inbound_creates_a_received_message():
    convo = ConversationFactory()
    msg = services.record_inbound(
        convo, channel=Message.Channel.SMS, body="Need a bus Friday", sender_name="Dave"
    )
    assert msg.conversation == convo
    assert msg.direction == Message.Direction.IN
    assert msg.delivery_status == Message.DeliveryStatus.RECEIVED
    assert msg.sent_at is None
    assert msg.sender_name == "Dave"


def test_record_outbound_creates_a_sent_message_with_sent_at():
    convo = ConversationFactory()
    msg = services.record_outbound(convo, channel=Message.Channel.EMAIL, body="Here's your quote")
    assert msg.direction == Message.Direction.OUT
    assert msg.delivery_status == Message.DeliveryStatus.SENT
    assert msg.sent_at is not None


def test_recording_stamps_last_message_at_on_the_conversation():
    convo = ConversationFactory()
    assert convo.last_message_at is None
    before = timezone.now()
    services.record_inbound(convo, channel=Message.Channel.SMS, body="hi")
    convo.refresh_from_db()
    assert convo.last_message_at is not None
    assert convo.last_message_at >= before


def test_later_message_advances_last_message_at():
    convo = ConversationFactory()
    services.record_inbound(convo, channel=Message.Channel.SMS, body="first")
    convo.refresh_from_db()
    first_stamp = convo.last_message_at
    services.record_outbound(convo, channel=Message.Channel.SMS, body="second")
    convo.refresh_from_db()
    assert convo.last_message_at >= first_stamp


def test_recording_does_not_reopen_an_archived_conversation():
    convo = ConversationFactory(status=Conversation.Status.ARCHIVED)
    services.record_inbound(convo, channel=Message.Channel.SMS, body="still spam")
    convo.refresh_from_db()
    assert convo.status == Conversation.Status.ARCHIVED
    assert convo.last_message_at is not None
