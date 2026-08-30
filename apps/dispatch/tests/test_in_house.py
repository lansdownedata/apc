from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory, VehicleFactory
from apps.fleet.models import Driver, Vehicle
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _booked_trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    return ReservationFactory(**kwargs)


# --- model ---


def test_in_house_rows_carry_a_driver_and_no_vendor():
    a = AssignmentFactory(in_house=True)
    assert a.vendor is None and a.driver is not None
    assert a.is_in_house is True
    assert a.status == Assignment.Status.CONFIRMED
    assert a.payout == 0
    assert a.provider_name == a.driver.name
    assert a.driver.name in str(a)


def test_vendor_rows_are_not_in_house():
    a = AssignmentFactory(vendor=VendorFactory(name="Capital Chauffeurs"))
    assert a.is_in_house is False
    assert a.provider_name == "Capital Chauffeurs"


def test_exactly_one_provider_is_enforced_by_the_database():
    trip = ReservationFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(reservation=trip)  # neither
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(reservation=trip, vendor=VendorFactory(), driver=DriverFactory())


def test_a_vehicle_needs_a_driver_at_the_database():
    trip = ReservationFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        Assignment.objects.create(
            reservation=trip, vendor=VendorFactory(), vehicle=VehicleFactory()
        )


# --- services ---


def test_assign_in_house_lands_confirmed_with_no_payout():
    trip, driver = _booked_trip(), DriverFactory()
    a = services.assign_in_house(trip, driver, note="his regular")
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at is not None
    assert a.channel == Assignment.Channel.MANUAL
    assert a.payout == Decimal("0")
    assert a.driver == driver and a.vehicle is None and a.vendor is None
    assert a.note == "his regular"
    assert services.active_assignment(trip) == a


def test_assign_in_house_with_a_unit():
    trip, driver, unit = _booked_trip(), DriverFactory(), VehicleFactory()
    a = services.assign_in_house(trip, driver, vehicle=unit)
    assert a.vehicle == unit


def test_in_house_refuses_an_inactive_driver_or_unit():
    trip = _booked_trip()
    with pytest.raises(services.AssignmentError, match="inactive"):
        services.assign_in_house(trip, DriverFactory(status=Driver.Status.INACTIVE))
    with pytest.raises(services.AssignmentError, match="inactive"):
        services.assign_in_house(
            trip, DriverFactory(), vehicle=VehicleFactory(status=Vehicle.Status.INACTIVE)
        )
    assert trip.assignments.count() == 0


def test_claim_refuses_zero_or_two_providers_and_a_vehicle_without_a_driver():
    trip = _booked_trip()
    kwargs = {"note": "", "status": Assignment.Status.CONFIRMED}
    with pytest.raises(services.AssignmentError):
        services._claim(trip, **kwargs)
    with pytest.raises(services.AssignmentError):
        services._claim(trip, vendor=VendorFactory(), driver=DriverFactory(), **kwargs)
    with pytest.raises(services.AssignmentError):
        services._claim(trip, vendor=VendorFactory(), vehicle=VehicleFactory(), **kwargs)


def test_in_house_respects_the_one_active_rule_across_provider_kinds():
    trip = _booked_trip()
    services.send_offer(trip, VendorFactory(), payout=Decimal("100.00"))
    with pytest.raises(services.AssignmentError, match="already"):
        services.assign_in_house(trip, DriverFactory())
    other = _booked_trip()
    services.assign_in_house(other, DriverFactory())
    with pytest.raises(services.AssignmentError, match="already"):
        services.send_offer(other, VendorFactory(), payout=Decimal("100.00"))


def test_in_house_still_needs_a_booked_uncancelled_trip():
    quoted = ReservationFactory(lead=LeadFactory(status=Lead.Status.QUOTED))
    with pytest.raises(services.AssignmentError, match="booked"):
        services.assign_in_house(quoted, DriverFactory())


def test_confirm_and_decline_are_refused_on_in_house_rows():
    a = AssignmentFactory(in_house=True)
    with pytest.raises(services.AssignmentError, match="unassigned"):
        services.confirm(a)
    with pytest.raises(services.AssignmentError, match="unassigned"):
        services.decline(a)
    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED


def test_withdraw_unassigns_an_in_house_row():
    a = AssignmentFactory(in_house=True)
    services.withdraw(a, note="driver sick")
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN
    assert services.active_assignment(a.reservation) is None


def test_release_trips_covers_in_house_rows():
    a = AssignmentFactory(in_house=True)
    released = services.release_trips([a.reservation], note="order cancelled")
    assert [r.pk for r in released] == [a.pk]


def test_existing_vendor_paths_pass_vendor_by_keyword():
    """send_offer / assign_direct must still work exactly as before the signature change."""
    trip = _booked_trip()
    a = services.assign_direct(trip, VendorFactory(), payout=Decimal("140.00"))
    assert a.vendor is not None and a.driver is None and a.is_in_house is False
