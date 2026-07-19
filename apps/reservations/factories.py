import factory

from apps.leads.factories import LeadFactory, VehicleTypeFactory

from .models import Reservation, Stop


class ReservationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Reservation
        skip_postgeneration_save = True

    lead = factory.SubFactory(LeadFactory)
    vehicle = factory.SubFactory(VehicleTypeFactory)
    trip_type = Reservation.TripType.TRANSFER
    service = "Transfer"
    passengers = 2
    base_rate = 185

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
    base_rate = 185


class HourlyReservationFactory(ReservationFactory):
    trip_type = Reservation.TripType.HOURLY
    service = "As-directed"
    hours = 5
    hourly_rate = 295
    min_hours = 4


class StopFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Stop

    reservation = factory.SubFactory(TransferReservationFactory)
    sequence = 1
    address = factory.Faker("street_address")
