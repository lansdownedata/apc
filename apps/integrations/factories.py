import factory

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory

from . import crypto
from .models import LACustomer, PodiumCredential, PodiumEvent, ZapEvent


class PodiumCredentialFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PodiumCredential

    organization_uid = "019ea8da-test"
    access_token = "access-token"
    refresh_token = "refresh-token"


class ZapEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ZapEvent

    lead = factory.SubFactory(LeadFactory)
    action = ZapEvent.Action.CREATE_RESERVATION
    idempotency_key = factory.Sequence(lambda n: f"zap-{n}")


class PodiumEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PodiumEvent

    event_type = PodiumEvent.EventType.MESSAGE_RECEIVED
    payload = factory.LazyFunction(dict)


class LACustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LACustomer

    contact = factory.SubFactory(ContactFactory)
    la_customer_id = factory.Sequence(lambda n: str(10000 + n))
    la_account_number = factory.Sequence(lambda n: f"9911{n:04d}")
    email_used = factory.LazyAttribute(lambda o: o.contact.email or "la@example.com")
    password_encrypted = factory.LazyFunction(lambda: crypto.encrypt("factory-pw"))
