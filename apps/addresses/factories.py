from decimal import Decimal

import factory

from .models import Address, Airline, Airport, Venue


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
    timezone = "America/New_York"
    # A plain factory-built airport represents an ordinary commercial one, matching the
    # existing test suite's assumption that it's both selectable and verifiable — tests
    # for the Andrews-style "no scheduled service" case override this explicitly.
    serves_ground_transport = True
    has_scheduled_service = True


class AirlineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Airline
        django_get_or_create = ("iata",)

    iata = factory.Sequence(lambda n: f"X{n % 100:02d}")
    icao = ""
    name = factory.Sequence(lambda n: f"Test Air {n}")
    is_active = True


class VenueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Venue

    name = factory.Sequence(lambda n: f"Test Venue {n}")
    kind = Venue.Kind.VENUE
    city = "Leesburg"
    state = "VA"
