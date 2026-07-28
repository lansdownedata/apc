import factory

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory

from .models import Conversation, Message, Review, TouchPoint


class ConversationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Conversation

    contact = factory.SubFactory(ContactFactory)


class MessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Message

    lead = factory.SubFactory(LeadFactory)
    # Derived from the lead's contact during the cut-over; the final state drops `lead`
    # and makes this a plain SubFactory(ConversationFactory).
    conversation = factory.LazyAttribute(
        lambda o: Conversation.objects.get_or_create(contact=o.lead.contact)[0]
    )
    direction = Message.Direction.IN
    channel = Message.Channel.SMS
    body = factory.Faker("sentence")


class TouchPointFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TouchPoint

    lead = factory.SubFactory(LeadFactory)
    kind = TouchPoint.Kind.TP1_WELCOME


class ReviewFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Review

    lead = factory.SubFactory(LeadFactory)
    contact = factory.SubFactory(ContactFactory)
