from datetime import timedelta

import factory
from django.utils import timezone

from apps.leads.factories import VehicleTypeFactory

from .models import Driver, Renewal, RenewalType, Vehicle


class DriverFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Driver

    name = factory.Faker("name")
    phone = factory.Faker("numerify", text="+1571555####")
    email = factory.Faker("email")


class VehicleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Vehicle

    name = factory.Sequence(lambda n: f"Unit {n + 1}")
    vehicle_type = factory.SubFactory(VehicleTypeFactory)
    license_plate = factory.Sequence(lambda n: f"APC-{n:04d}")


class RenewalTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RenewalType
        django_get_or_create = ("name", "applies_to")

    name = factory.Sequence(lambda n: f"Renewal type {n}")
    applies_to = RenewalType.AppliesTo.DRIVER


class RenewalFactory(factory.django.DjangoModelFactory):
    """A driver-subject renewal by default; pass driver=None, vehicle=… for a unit."""

    class Meta:
        model = Renewal

    renewal_type = factory.SubFactory(RenewalTypeFactory)
    driver = factory.SubFactory(DriverFactory)
    reference = factory.Sequence(lambda n: f"REF-{n}")
    expires_on = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=180))


class VehicleRenewalFactory(RenewalFactory):
    driver = None
    vehicle = factory.SubFactory(VehicleFactory)
    renewal_type = factory.SubFactory(RenewalTypeFactory, applies_to=RenewalType.AppliesTo.VEHICLE)
