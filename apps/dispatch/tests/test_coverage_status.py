"""Coverage drives the trip status: an offer shows Assigned, acceptance or direct coverage
shows Dispatched, and two computed badges read the assignment."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.dispatch import gnet_sync, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db
TS = Reservation.TripStatus


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("trip_status", TS.UNASSIGNED)
    return ReservationFactory(**kwargs)


def _status(trip):
    trip.refresh_from_db()
    return trip.trip_status


def test_offer_sets_trip_assigned(mailoutbox):
    trip = _trip()
    services.send_offer(trip, VendorFactory(email="ops@x.example"), payout=Decimal("100"))
    assert _status(trip) == TS.ASSIGNED


def test_gnet_offer_sets_trip_assigned():
    trip = _trip()
    vendor = VendorFactory(gnet_grid_id="gnetvendor")
    with patch.object(gnet_sync, "push_assignment"):
        services.send_offer(trip, vendor, payout=Decimal("100"))
    assert _status(trip) == TS.ASSIGNED


def test_offer_never_moves_a_dispatched_trip_back(mailoutbox):
    trip = _trip(trip_status=TS.DISPATCHED)
    services.send_offer(trip, VendorFactory(email="ops@x.example"), payout=Decimal("100"))
    assert _status(trip) == TS.DISPATCHED


def test_confirm_sets_dispatched_and_stamps_offer_accepted_at(mailoutbox):
    trip = _trip()
    offer = services.send_offer(trip, VendorFactory(email="ops@x.example"), payout=Decimal("100"))
    assert offer.offer_accepted_at is None
    confirmed = services.confirm(offer)
    confirmed.refresh_from_db()
    assert confirmed.offer_accepted_at is not None
    assert _status(trip) == TS.DISPATCHED


def test_assign_direct_and_in_house_go_straight_to_dispatched():
    direct, house = _trip(), _trip()
    a = services.assign_direct(direct, VendorFactory(), payout=Decimal("100"))
    b = services.assign_in_house(house, DriverFactory())
    assert _status(direct) == TS.DISPATCHED
    assert _status(house) == TS.DISPATCHED
    assert a.offer_accepted_at is None
    assert b.offer_accepted_at is None


def test_confirm_never_moves_an_under_way_trip_back():
    trip = _trip(trip_status=TS.ON_THE_WAY)
    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    services.confirm(offer)
    assert _status(trip) == TS.ON_THE_WAY


def test_any_claim_clears_needs_farm_out():
    """Coverage of any kind, our own driver included, ends the need for an affiliate."""
    direct, house = _trip(needs_farm_out=True), _trip(needs_farm_out=True)
    services.assign_direct(direct, VendorFactory(), payout=Decimal("100"))
    services.assign_in_house(house, DriverFactory())
    for trip in (direct, house):
        trip.refresh_from_db()
        assert trip.needs_farm_out is False


def test_a_blank_status_still_moves_to_dispatched():
    """A row written blank by an old dyno during the release window still moves."""
    trip = _trip()
    trip.trip_status = ""
    trip.save(update_fields=["trip_status"])
    services.assign_in_house(trip, DriverFactory())
    assert _status(trip) == TS.DISPATCHED


def test_badges():
    trip = _trip(trip_status=TS.ASSIGNED)
    vendor = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    assert vendor.is_affiliate_coverage is True
    assert vendor.driver_assigned is False
    vendor.driver_info_at = vendor.created_at
    assert vendor.driver_assigned is True

    trip.trip_status = TS.PENDING
    assert vendor.is_affiliate_coverage is False
    trip.trip_status = TS.DISPATCHED
    assert vendor.is_affiliate_coverage is True
    vendor.status = Assignment.Status.DECLINED
    assert vendor.is_affiliate_coverage is False

    house = AssignmentFactory(in_house=True, reservation=_trip(trip_status=TS.DISPATCHED))
    assert house.is_affiliate_coverage is False
    assert house.driver_assigned is True
