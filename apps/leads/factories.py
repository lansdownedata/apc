import factory

from apps.contacts.factories import ContactFactory

from .models import Lead, ServiceType, VehicleType


class ServiceTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ServiceType
        django_get_or_create = ("name",)

    name = "Airport Transfer"
    active = True
    sort_order = 0


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
    transfer_min_hours = 1


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lead

    contact = factory.SubFactory(ContactFactory)
