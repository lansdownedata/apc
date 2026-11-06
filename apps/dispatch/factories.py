import factory

from apps.contacts.factories import CompanyFactory
from apps.fleet.factories import DriverFactory
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

from .models import Assignment, FarmInRequest, FarmInStatusPush, GnetEvent


class AssignmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Assignment

    reservation = factory.SubFactory(ReservationFactory)
    vendor = factory.SubFactory(VendorFactory)
    payout = 140

    class Params:
        in_house = factory.Trait(
            vendor=None,
            driver=factory.SubFactory(DriverFactory),
            status=Assignment.Status.CONFIRMED,
            payout=0,
        )


class GnetEventFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = GnetEvent

    assignment = factory.SubFactory(AssignmentFactory)
    action = GnetEvent.Action.SEND_TRIP
    idempotency_key = factory.Sequence(lambda n: f"gnet-event-{n}")


class FarmInRequestFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FarmInRequest

    company = factory.SubFactory(CompanyFactory, is_affiliate_account=True)
    transaction_id = factory.Sequence(lambda n: f"tx-{n}")
    requester_grid_id = "gnettest"
    payload = factory.LazyFunction(dict)


class FarmInStatusPushFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FarmInStatusPush

    request = factory.SubFactory(FarmInRequestFactory)
    status = "CONFIRMED"
