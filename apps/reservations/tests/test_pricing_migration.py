"""The pricing migration must not change any existing reservation's line_total."""

from decimal import Decimal

import pytest

from apps.reservations.factories import HourlyReservationFactory, ReservationFactory

pytestmark = pytest.mark.django_db


def test_transfer_total_preserved_via_rate_times_one():
    # a transfer priced at a flat 240 → rate 240, hours 1 → subtotal 240
    r = ReservationFactory(trip_type="transfer", rate=Decimal("240.00"), hours=Decimal("1"))
    assert r.line_total == Decimal("240.00")


def test_hourly_total_uses_the_override_when_one_is_stored():
    r = HourlyReservationFactory(rate=Decimal("295.00"), hours=Decimal("3"), min_hours=Decimal("4"))
    assert r.line_total == Decimal("885.00")  # override 3 replaces the 4-hr minimum
