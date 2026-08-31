"""Turning the customer-facing recommendation into a real, priced VehicleType."""

from decimal import Decimal

import pytest

from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.leads.services import apply_vehicle_rate_card, suggest_vehicle
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _empty_catalog():
    """reservations.0003 seeds a starter catalog into every test DB, and
    VehicleTypeFactory does get_or_create on `name` — so without this the fixture below
    silently reuses seeded rows and never applies its own capacities."""
    VehicleType.objects.all().delete()


@pytest.fixture
def fleet(_empty_catalog):
    """The catalog shape the client actually runs, smallest first."""
    return {
        "suv": VehicleTypeFactory(name="Luxury SUV", capacity=6, sort_order=0),
        "van": VehicleTypeFactory(name="Sprinter Van", capacity=14, sort_order=1),
        "mini": VehicleTypeFactory(name="28-Passenger Mini Coach", capacity=28, sort_order=2),
        "coach40": VehicleTypeFactory(name="40-Passenger Coach", capacity=40, sort_order=3),
        "coach56": VehicleTypeFactory(name="55-Passenger Motorcoach", capacity=56, sort_order=4),
    }


def test_the_smallest_vehicle_that_seats_the_run_wins(fleet):
    assert suggest_vehicle(4) == fleet["suv"]
    assert suggest_vehicle(7) == fleet["van"]
    assert suggest_vehicle(28) == fleet["mini"]


def test_a_venue_cap_sizes_the_run_not_the_headcount(fleet):
    """105 guests at a 40-cap venue is three 40-seat runs, not one 105-seat vehicle."""
    assert suggest_vehicle(105, 40) == fleet["coach40"]


def test_without_a_cap_a_big_group_splits_across_our_largest_coach(fleet):
    assert suggest_vehicle(105, None) == fleet["coach56"]


def test_nothing_big_enough_suggests_nothing(fleet):
    """The picker opens unset rather than guessing."""
    fleet["coach56"].delete()
    fleet["coach40"].delete()
    assert suggest_vehicle(50) is None


def test_a_retired_vehicle_is_never_suggested(fleet):
    fleet["suv"].active = False
    fleet["suv"].save()
    assert suggest_vehicle(4) == fleet["van"]


def test_ties_on_capacity_break_on_sort_order(fleet):
    """Two vehicles seating the same number: the catalog's own order decides."""
    fleet["suv"].sort_order = 9
    fleet["suv"].save()
    preferred = VehicleTypeFactory(name="Preferred SUV", capacity=6, sort_order=0)
    assert suggest_vehicle(4) == preferred


def test_the_rate_card_is_snapshotted_off_the_vehicle(fleet):
    vehicle = fleet["coach40"]
    vehicle.rate = Decimal("150.00")
    vehicle.transfer_min_hours = Decimal("3.00")
    vehicle.save()
    res = ReservationFactory(rate=0, min_hours=0)
    apply_vehicle_rate_card(res, vehicle)
    assert res.vehicle == vehicle
    assert res.rate == Decimal("150.00")
    assert res.min_hours == Decimal("3.00")


def test_clearing_the_vehicle_clears_the_rate_card(fleet):
    res = ReservationFactory(vehicle=fleet["coach40"], rate=Decimal("150.00"), min_hours=3)
    apply_vehicle_rate_card(res, None)
    assert res.vehicle is None
    assert res.rate == 0
    assert res.min_hours == 0


def test_an_hourly_trip_snapshots_the_hourly_minimum_instead(fleet):
    """The minimum follows the trip type, exactly as the reservation editor does it —
    an hourly wedding leg billed at the transfer minimum would quote hours short."""
    vehicle = fleet["coach40"]
    vehicle.rate = Decimal("150.00")
    vehicle.transfer_min_hours = Decimal("3.00")
    vehicle.hourly_min_hours = Decimal("8.00")
    vehicle.save()
    res = ReservationFactory(trip_type="hourly", rate=0, min_hours=0)
    apply_vehicle_rate_card(res, vehicle)
    assert res.min_hours == Decimal("8.00")


def test_a_transfer_still_snapshots_the_transfer_minimum(fleet):
    vehicle = fleet["coach40"]
    vehicle.transfer_min_hours = Decimal("3.00")
    vehicle.hourly_min_hours = Decimal("8.00")
    vehicle.save()
    res = ReservationFactory(trip_type="transfer", min_hours=0)
    apply_vehicle_rate_card(res, vehicle)
    assert res.min_hours == Decimal("3.00")


def test_the_snapshot_produces_the_same_subtotal_the_editor_would(fleet):
    """Wedding legs default to transfers, so the minimum is transfer_min_hours."""
    vehicle = fleet["coach40"]
    vehicle.rate = Decimal("150.00")
    vehicle.transfer_min_hours = Decimal("3.00")
    vehicle.hourly_min_hours = Decimal("8.00")
    vehicle.save()
    res = ReservationFactory(trip_type="transfer", hours=0)
    apply_vehicle_rate_card(res, vehicle)
    assert res.subtotal == Decimal("450.00")
