import factory

from .models import Vendor


class VendorFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Vendor

    name = factory.Sequence(lambda n: f"Vendor {n}")
    contact_name = factory.Faker("name")
    email = factory.Faker("company_email")
    phone = factory.Faker("numerify", text="+1617555####")
    service_area = "Washington DC metro"
