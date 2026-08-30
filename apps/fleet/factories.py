import factory

from .models import Driver


class DriverFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Driver

    name = factory.Faker("name")
    phone = factory.Faker("numerify", text="+1571555####")
    email = factory.Faker("email")
