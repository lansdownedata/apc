import factory

from apps.contacts.factories import ContactFactory

from .models import Lead, VehicleType


class VehicleTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = VehicleType
        django_get_or_create = ("name",)

    name = "Luxury SUV"
    capacity = 6
    description = ""
    sort_order = 0
    rate = 0
    hourly_min_hours = 0
    transfer_min_hours = 0


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lead

    contact = factory.SubFactory(ContactFactory)
