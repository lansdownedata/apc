"""Cost-based pricing arithmetic — discount, order of operations, dime rounding.

Spec: docs/specs/2026-09-05-cost-based-pricing-design.md §3.2, §4.5.

The discount fields have existed (and been persisted, and been round-trip tested) since the
rate-card work, but `line_total` never applied them. These tests are what makes them real.
"""

from decimal import Decimal

import pytest

from apps.reservations.factories import HourlyReservationFactory, ReservationFactory
from apps.reservations.models import round_to_dime

pytestmark = pytest.mark.django_db


# --- discount ---------------------------------------------------------------
def test_flat_discount_comes_off_the_base():
    r = ReservationFactory(rate=Decimal("1000"), hours=Decimal("1"), discount_flat=Decimal("25"))
    assert r.discount == Decimal("25.00")
    assert r.line_total == Decimal("975.00")


def test_percent_discount_comes_off_the_base():
    r = ReservationFactory(rate=Decimal("1000"), hours=Decimal("1"), discount_pct=Decimal("10"))
    assert r.discount == Decimal("100.00")
    assert r.line_total == Decimal("900.00")


def test_flat_discount_wins_when_both_are_set():
    """Mirrors the established `gratuity_flat` precedent — flat is an override."""
    r = ReservationFactory(
        rate=Decimal("1000"),
        hours=Decimal("1"),
        discount_pct=Decimal("10"),
        discount_flat=Decimal("25"),
    )
    assert r.discount == Decimal("25.00")


def test_gratuity_is_computed_on_the_discounted_base():
    """You don't tip on money the customer didn't pay: 20% of 900, not of 1000."""
    r = ReservationFactory(
        rate=Decimal("1000"),
        hours=Decimal("1"),
        discount_pct=Decimal("10"),
        gratuity_pct=Decimal("20"),
    )
    assert r.discounted_base == Decimal("900.00")
    assert r.gratuity == Decimal("180.00")
    assert r.line_total == Decimal("1080.00")


def test_a_discount_larger_than_the_base_clamps_to_zero():
    """A negative line total would post a negative ledger entry — money bug, not display."""
    r = ReservationFactory(rate=Decimal("1000"), hours=Decimal("1"), discount_flat=Decimal("2000"))
    assert r.discounted_base == Decimal("0.00")
    assert r.line_total == Decimal("0.00")


# --- no historical price moves ---------------------------------------------
# Every discount in the database is 0 today, so `discounted_base == subtotal` for every
# existing row. These expected values are hard-coded on purpose: a test that re-derives the
# new formula would agree with itself no matter what the refactor broke.
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"rate": "185", "hours": "1", "min_hours": "0"}, "185.00"),
        ({"rate": "240", "hours": "5", "min_hours": "4"}, "1200.00"),
        ({"rate": "300", "hours": "0", "min_hours": "4"}, "1200.00"),
        ({"rate": "100", "hours": "5", "min_hours": "4", "gratuity_pct": "20"}, "600.00"),
        ({"rate": "100", "hours": "2", "min_hours": "0", "gratuity_flat": "75"}, "275.00"),
        ({"rate": "100", "hours": "3", "min_hours": "0"}, "300.00"),
        ({"rate": "300", "hours": "0", "min_hours": "0"}, "0.00"),
    ],
)
def test_existing_prices_do_not_move(kwargs, expected):
    r = ReservationFactory(**{k: Decimal(v) for k, v in kwargs.items()})
    assert r.line_total == Decimal(expected)


# --- dime rounding ----------------------------------------------------------
def test_rounds_up_to_the_next_dime():
    assert round_to_dime(Decimal("1538.4615")) == Decimal("1538.50")
    assert round_to_dime(Decimal("1538.41")) == Decimal("1538.50")
    assert round_to_dime(Decimal("1538.49")) == Decimal("1538.50")


def test_rounding_never_goes_down():
    """Rounding down would price below the target ratio. Costs at most 9c."""
    for cents in range(1, 10):
        amount = Decimal("1000") + (Decimal(cents) / 100)
        assert round_to_dime(amount) == Decimal("1000.10")


def test_rounding_is_idempotent():
    """The rate is stored and re-read on every edit — re-rounding must never drift."""
    once = round_to_dime(Decimal("1538.4615"))
    assert round_to_dime(once) == once == Decimal("1538.50")


def test_rounding_handles_zero_and_none():
    assert round_to_dime(Decimal("0")) == Decimal("0.00")
    assert round_to_dime(None) == Decimal("0.00")


def test_an_exact_dime_is_left_alone():
    assert round_to_dime(Decimal("1538.50")) == Decimal("1538.50")
    assert round_to_dime(Decimal("1538.00")) == Decimal("1538.00")


# --- the discount reaches an hourly trip too --------------------------------
def test_discount_applies_to_an_hourly_trip():
    r = HourlyReservationFactory(
        rate=Decimal("200"), hours=Decimal("5"), min_hours=Decimal("4"), discount_pct=Decimal("10")
    )
    assert r.subtotal == Decimal("1000.00")
    assert r.line_total == Decimal("900.00")
