"""The pricing migration must not change any existing reservation's line_total."""

import importlib
from decimal import Decimal

import pytest

from apps.reservations.factories import HourlyReservationFactory, ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def test_transfer_total_preserved_via_rate_times_one():
    # a transfer priced at a flat 240 → rate 240, hours 1 → subtotal 240
    r = ReservationFactory(trip_type="transfer", rate=Decimal("240.00"), hours=Decimal("1"))
    assert r.line_total == Decimal("240.00")


def test_hourly_total_uses_the_override_when_one_is_stored():
    r = HourlyReservationFactory(rate=Decimal("295.00"), hours=Decimal("3"), min_hours=Decimal("4"))
    assert r.line_total == Decimal("885.00")  # override 3 replaces the 4-hr minimum


def test_migration_resets_sub_minimum_overrides_so_totals_are_preserved():
    """0 < hours < min_hours billed the minimum under the old max() rule; under the new
    rule only hours=0 does. The migration moves those rows, and only those rows."""
    floored = HourlyReservationFactory(
        rate=Decimal("295.00"), hours=Decimal("3"), min_hours=Decimal("4")
    )
    above = HourlyReservationFactory(
        rate=Decimal("295.00"), hours=Decimal("6"), min_hours=Decimal("4")
    )
    none = HourlyReservationFactory(
        rate=Decimal("295.00"), hours=Decimal("0"), min_hours=Decimal("4")
    )
    module = importlib.import_module(
        "apps.reservations.migrations.0008_min_applied_rows_become_no_override"
    )
    assert module.blank_sub_minimum_overrides(Reservation) == 1
    for r in (floored, above, none):
        r.refresh_from_db()
    assert floored.hours == 0 and floored.line_total == Decimal("1180.00")  # 4 × 295, as before
    assert above.hours == Decimal("6") and above.line_total == Decimal("1770.00")
    assert none.hours == 0
