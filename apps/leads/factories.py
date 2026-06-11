import factory

from apps.contacts.factories import ContactFactory

from .models import Lead, Vehicle


class VehicleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Vehicle
        django_get_or_create = ("name",)

    name = "Luxury SUV"
    capacity = 6
    klass = Vehicle.Klass.SUV


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lead

    contact = factory.SubFactory(ContactFactory)
