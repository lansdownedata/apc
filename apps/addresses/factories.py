import factory

from .models import Address


class AddressFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Address
