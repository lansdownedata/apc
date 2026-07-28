from decimal import Decimal

import factory

from .models import Address, Airport


class AddressFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Address


class AirportFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Airport

    ourairports_id = factory.Sequence(lambda n: 90000 + n)
    ident = factory.Sequence(lambda n: f"KT{n:02d}")
    iata = factory.Sequence(lambda n: f"T{n:02d}")
    icao = ""
    size = Airport.Size.LARGE
    name = factory.Sequence(lambda n: f"Test Airport {n}")
    city = "Testville"
    state = "VA"
    country = "US"
    latitude = Decimal("38.851242")
    longitude = Decimal("-77.037720")
    elevation_ft = 15
