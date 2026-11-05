import factory

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory

from .models import Conversation, Delivery, Message, MessageTemplate, Review, TouchPoint


class ConversationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Conversation

    contact = factory.SubFactory(ContactFactory)


class MessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Message

    conversation = factory.SubFactory(ConversationFactory)
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


class MessageRowFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = MessageTemplate

    key = factory.Sequence(lambda n: f"custom-{n}")
    name = factory.Sequence(lambda n: f"Message {n}")
    section = MessageTemplate.Section.LEADS_QUOTES
    audience = MessageTemplate.Audience.CUSTOMER
    trigger = "lead_created"
    timing_amount = 0
    timing_unit = "minutes"


class DeliveryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Delivery

    message = factory.SubFactory(MessageRowFactory)
    touchpoint = factory.SubFactory(TouchPointFactory, message=factory.SelfAttribute("..message"))
    channel = Delivery.Channel.SMS
    address = "+15555550100"
