from datetime import timedelta

import factory
from django.utils import timezone

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory

from .models import Flight, FlightDirection, Reservation, Stop


class ReservationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Reservation
        skip_postgeneration_save = True

    lead = factory.SubFactory(LeadFactory)
    vehicle = factory.SubFactory(VehicleTypeFactory)
    trip_type = Reservation.TripType.TRANSFER
    passengers = 2
    rate = 185
    hours = 1  # transfer default → subtotal = rate * 1

    @factory.post_generation
    def stops(self, create, extracted, **kwargs):
        if not create:
            return
        if extracted is not None:
            for i, address in enumerate(extracted):
                Stop.objects.create(reservation=self, sequence=i, address=address)
        else:
            Stop.objects.create(reservation=self, sequence=0, address="Pickup")
            Stop.objects.create(reservation=self, sequence=1, address="Drop-off")


class TransferReservationFactory(ReservationFactory):
    trip_type = Reservation.TripType.TRANSFER


class HourlyReservationFactory(ReservationFactory):
    trip_type = Reservation.TripType.HOURLY
    rate = 295
    hours = 5
    min_hours = 4


class StopFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Stop

    reservation = factory.SubFactory(TransferReservationFactory)
    sequence = 1
    address = factory.Faker("street_address")


class FlightFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Flight

    airline = factory.SubFactory(AirlineFactory)
    flight_number = "123"
    flight_date = factory.LazyFunction(lambda: timezone.localdate() + timedelta(days=30))
    airport = factory.SubFactory(AirportFactory)
    direction = FlightDirection.ARRIVAL
    status = Flight.Status.SCHEDULED
    scheduled_at = factory.LazyFunction(lambda: timezone.now() + timedelta(hours=1))
    source = Flight.Source.FUTURE
    checked_at = factory.LazyFunction(timezone.now)
