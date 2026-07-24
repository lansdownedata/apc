import factory

from .models import Vendor, VendorDocument, VendorDriver


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
