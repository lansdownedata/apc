from datetime import timedelta

import factory
from django.utils import timezone

from .models import Vendor, VendorDocument, VendorDriver, VendorInsurance


class VendorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Vendor

    name = factory.Sequence(lambda n: f"Vendor {n}")
    contact_name = factory.Faker("name")
    email = factory.Faker("company_email")
    phone = factory.Faker("numerify", text="+1617555####")
    service_area = "Washington DC metro"


class VendorDriverFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = VendorDriver

    vendor = factory.SubFactory(VendorFactory)
    name = factory.Faker("name")


class VendorDocumentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = VendorDocument

    vendor = factory.SubFactory(VendorFactory)
    label = "W-9"
    file = factory.django.FileField(filename="w9.pdf")


class VendorInsuranceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = VendorInsurance

    vendor = factory.SubFactory(VendorFactory)
    insurer = "Acme Mutual"
    policy_number = factory.Sequence(lambda n: f"P-{n}")
    coverage_amount = 1_000_000
    effective_date = factory.LazyFunction(lambda: timezone.localdate() - timedelta(days=365))
    expiry_date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=180))
