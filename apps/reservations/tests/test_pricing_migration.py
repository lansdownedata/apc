"""The pricing migration must not change any existing reservation's line_total."""

from decimal import Decimal

import pytest

from apps.reservations.factories import HourlyReservationFactory, ReservationFactory

pytestmark = pytest.mark.django_db


def test_transfer_total_preserved_via_rate_times_one():
    # a transfer priced at a flat 240 → rate 240, hours 1 → subtotal 240
    r = ReservationFactory(trip_type="transfer", rate=Decimal("240.00"), hours=Decimal("1"))
    assert r.line_total == Decimal("240.00")


def test_hourly_total_matches_old_billed_hours_times_rate():
    r = HourlyReservationFactory(rate=Decimal("295.00"), hours=Decimal("3"), min_hours=Decimal("4"))
    assert r.line_total == Decimal("1180.00")  # max(3,4) * 295
