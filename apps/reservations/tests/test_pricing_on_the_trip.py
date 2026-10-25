"""Overtime and extras live on the trip (spec 2026-10-25 §3, APC-100).

`line_total` is the one truth for the order page, the receipt and the invoice line, so
overtime hours and flat extras are stored on the Reservation and folded into it here.
"""

from decimal import Decimal

import pytest

from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import TripExtra

pytestmark = pytest.mark.django_db


def _hourly(**kw):
    return ReservationFactory(trip_type="hourly", rate=Decimal("185"), hours=Decimal("6"), **kw)


# --- overtime ---------------------------------------------------------------
def test_overtime_bills_at_the_discounted_rate():
    r = _hourly(overtime_hours=Decimal("2"), discount_pct=Decimal("10"))

    assert r.discounted_rate == Decimal("166.50")
    assert r.overtime_amount == Decimal("333.00")


def test_percent_gratuity_covers_base_and_overtime():
    r = _hourly(overtime_hours=Decimal("2"), gratuity_pct=Decimal("20"))

    assert r.gratuity == Decimal("296.00")  # (1110 + 370) × 20%
    assert r.overtime_gratuity == Decimal("74.00")
    assert r.line_total == Decimal("1776.00")


def test_a_flat_gratuity_adds_nothing_on_overtime():
    r = _hourly(overtime_hours=Decimal("2"), gratuity_flat=Decimal("100"))

    assert r.gratuity == Decimal("100.00")
    assert r.overtime_gratuity == Decimal("0.00")


# --- extras -----------------------------------------------------------------
def test_extras_add_and_a_credit_subtracts():
    r = _hourly()
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.CLEANING, label="Cleaning fee", amount=Decimal("250")
    )
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.CREDIT, label="Courtesy", amount=Decimal("50")
    )

    assert r.extras_total == Decimal("200.00")
    assert r.line_total == Decimal("1310.00")


def test_a_credit_larger_than_the_trip_floors_the_total_at_zero():
    r = _hourly()
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.CREDIT, amount=Decimal("5000"))

    assert r.line_total == Decimal("0.00")


def test_extras_do_not_earn_gratuity():
    r = _hourly(gratuity_pct=Decimal("20"))
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.TOLLS, amount=Decimal("40"))

    assert r.gratuity == Decimal("222.00")


def test_an_extra_gets_its_kind_label_when_none_is_given():
    assert TripExtra.Kind.default_label(TripExtra.Kind.CLEANING) == "Cleaning fee"
    assert TripExtra.Kind.default_label(TripExtra.Kind.CREDIT) == "Credit"


# --- what the customer sees -------------------------------------------------
def test_the_lines_still_sum_to_the_line_total():
    r = _hourly(
        overtime_hours=Decimal("1"), gratuity_pct=Decimal("20"), discount_flat=Decimal("10")
    )
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.PARKING, label="Parking", amount=Decimal("15")
    )
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.CREDIT, label="Credit", amount=Decimal("5"))

    lines = r.price_lines()

    assert sum(line.amount for line in lines) == r.line_total
    labels = [line.label for line in lines]
    assert "Overtime (1 hr)" in labels
    assert "Parking" in labels
    assert [line for line in lines if line.label == "Credit"][0].amount == Decimal("-5.00")


def test_a_transfer_names_its_overtime_wait_time():
    r = ReservationFactory(
        trip_type="transfer", rate=Decimal("185"), hours=Decimal("1"), overtime_hours=Decimal("2")
    )

    assert "Wait time (2 hrs)" in [line.label for line in r.price_lines()]


def test_overtime_or_an_extra_counts_as_extras_for_the_customer_copy():
    assert _hourly(overtime_hours=Decimal("1")).has_extras is True
    r = _hourly()
    TripExtraFactory(reservation=r)
    assert r.has_extras is True


def test_a_plain_price_still_has_no_extras():
    assert _hourly().has_extras is False


# --- booked total -----------------------------------------------------------
def test_booked_total_is_null_until_frozen():
    assert _hourly().booked_total is None
