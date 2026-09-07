"""An estimated drop-off never reaches an affiliate.

We now derive an end time for every transfer so dispatch and our own screens stop showing
an open-ended job. That estimate is ours; in a GNet farm-out payload a drop-off time reads
as a commitment the affiliate is held to, so only a time an agent actually typed goes out.
"""

from datetime import date, time
from decimal import Decimal

import pytest

from apps.dispatch.factories import AssignmentFactory
from apps.integrations import gnet
from apps.leads.factories import VehicleTypeFactory
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _dropoff(**over):
    reservation = ReservationFactory(
        vehicle=VehicleTypeFactory(name="Luxury Sedan"),
        passengers=3,
        rate=Decimal("500.00"),
        pickup_date=date(2026, 9, 1),
        pickup_time=time(14, 0),
        dropoff_date=date(2026, 9, 1),
        dropoff_time=time(16, 0),
        stops=["1600 Pennsylvania Ave NW", "IAD Airport"],
        **over,
    )
    assignment = AssignmentFactory(
        reservation=reservation,
        vendor=VendorFactory(gnet_grid_id="gnet-partner-42"),
        payout=Decimal("300.00"),
    )
    return gnet.build_send_payload(assignment)["locations"]


def test_an_agent_typed_dropoff_is_sent():
    assert _dropoff(dropoff_estimated=False)["dropOff"]["time"]


def test_an_estimated_dropoff_is_withheld():
    assert not _dropoff(dropoff_estimated=True)["dropOff"].get("time")


def test_the_pickup_time_is_sent_either_way():
    """Only the end is an estimate — the pickup is what the customer booked."""
    assert _dropoff(dropoff_estimated=True)["pickup"]["time"]
