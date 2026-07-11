import factory

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory

from .models import Message, Review, TouchPoint


class MessageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Message

    lead = factory.SubFactory(LeadFactory)
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
