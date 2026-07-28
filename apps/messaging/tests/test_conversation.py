import pytest
from django.db import IntegrityError

from apps.contacts.factories import ContactFactory
from apps.messaging.factories import ConversationFactory
from apps.messaging.models import Conversation

pytestmark = pytest.mark.django_db


def test_conversation_defaults_to_open():
    convo = ConversationFactory()
    assert convo.status == Conversation.Status.OPEN
    assert convo.last_message_at is None
    assert convo.archived_at is None
    assert convo.archived_by is None


def test_contact_has_at_most_one_conversation():
    contact = ContactFactory()
    ConversationFactory(contact=contact)
    with pytest.raises(IntegrityError):
        ConversationFactory(contact=contact)


def test_conversation_is_reachable_from_its_contact():
    contact = ContactFactory()
    convo = ConversationFactory(contact=contact)
    assert contact.conversation == convo


def test_str_names_the_contact():
    convo = ConversationFactory(contact=ContactFactory(name="Dave Mercer"))
    assert str(convo) == "Conversation · Dave Mercer"
