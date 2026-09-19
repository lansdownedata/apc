"""The shuttle catalog: which vehicles count as group transport, and the biggest of them."""

import pytest

from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.leads.services import group_fleet, largest_group_capacity

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


def test_group_transport_defaults_on_so_a_new_vehicle_is_usable(fleet):
    """A vehicle added in Settings shuttles until someone says otherwise — the opposite
    default would silently shrink the catalog a point of interest can be capped to."""
    added = VehicleTypeFactory(name="Executive Minibus", capacity=30)
    assert added.group_transport is True
    assert added in group_fleet()


def test_the_group_fleet_is_the_active_shuttle_catalog_smallest_first(fleet):
    fleet["van"].active = False
    fleet["van"].save()
    VehicleTypeFactory(name="Party Limo Bus", capacity=20, group_transport=False)
    assert [v.capacity for v in group_fleet()] == [6, 28, 40, 56]


def test_the_largest_group_vehicle_is_the_default_ceiling(fleet):
    """No POI cap on file means our biggest coach, not a hardcoded 56."""
    assert largest_group_capacity() == 56
    fleet["coach56"].delete()
    assert largest_group_capacity() == 40
