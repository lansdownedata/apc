import factory

from apps.core.choices import Channel

from .models import Company, Contact


class CompanyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Company

    name = factory.Faker("company")


class ContactFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Contact

    name = factory.Faker("name")
    email = factory.Faker("email")
    phone = factory.Faker("numerify", text="(###) ###-####")
    channel = Channel.WEBSITE
