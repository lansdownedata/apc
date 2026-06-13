import factory

from apps.leads.factories import LeadFactory

from .models import Notification


class NotificationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Notification

    lead = factory.SubFactory(LeadFactory)
    kind = Notification.Kind.BALANCE_FAILED
    title = "Balance charge failed"
