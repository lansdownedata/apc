import factory

from apps.leads.factories import LeadFactory

from .models import PodiumCredential, PodiumEvent, ZapEvent


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
