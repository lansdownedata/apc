from decimal import Decimal

import pytest

from apps.reservations.factories import (
    HourlyReservationFactory,
    ReservationFactory,
    StopFactory,
    TransferReservationFactory,
)
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation, TripStatusEvent

pytestmark = pytest.mark.django_db


# --- pricing ---------------------------------------------------------------
def test_transfer_line_total_is_base_rate():
    res = TransferReservationFactory(rate=Decimal("185"))
    assert res.line_total == Decimal("185.00")


def test_hourly_line_total_is_hours_times_rate():
    res = HourlyReservationFactory(hours=Decimal("5"), rate=Decimal("240"), min_hours=Decimal("4"))
    assert res.billed_hours == Decimal("5")
    assert res.min_applied is False
    assert res.line_total == Decimal("1200.00")


# --- override hours vs rate-card minimum (spec 2026-08-28) ----------------
def test_no_override_bills_the_rate_card_minimum():
    res = HourlyReservationFactory(hours=Decimal("0"), rate=Decimal("300"), min_hours=Decimal("4"))
    assert res.billed_hours == Decimal("4")
    assert res.min_applied is True
    assert res.line_total == Decimal("1200.00")


def test_override_replaces_the_minimum_even_when_lower():
    res = HourlyReservationFactory(hours=Decimal("2"), rate=Decimal("300"), min_hours=Decimal("4"))
    assert res.billed_hours == Decimal("2")
    assert res.min_applied is False
    assert res.line_total == Decimal("600.00")


def test_no_override_and_no_minimum_bills_nothing():
    res = HourlyReservationFactory(hours=Decimal("0"), rate=Decimal("300"), min_hours=Decimal("0"))
    assert res.billed_hours == Decimal("0")
    assert res.min_applied is False
    assert res.line_total == Decimal("0.00")


def test_transfer_is_rate_times_its_minimum_by_default():
    res = TransferReservationFactory(
        rate=Decimal("240"), hours=Decimal("0"), min_hours=Decimal("1")
    )
    assert res.line_total == Decimal("240.00")


# --- routing ---------------------------------------------------------------
def test_pickup_and_dropoff_from_stops():
    res = TransferReservationFactory()
    assert res.pickup.address == "Pickup"
    assert res.dropoff.address == "Drop-off"
    assert res.is_multi_stop is False


def test_is_multi_stop_with_intermediate_stop():
    res = TransferReservationFactory()
    StopFactory(reservation=res, sequence=2, address="Photo stop")
    assert res.is_multi_stop is True


# --- trip status / phase ---------------------------------------------------
def test_trip_phase_maps_from_status():
    res = TransferReservationFactory(trip_status=Reservation.TripStatus.ON_THE_WAY)
    assert res.trip_phase == "En Route to Pickup"


def test_no_show_is_in_cancelled_phase():
    res = TransferReservationFactory(trip_status=Reservation.TripStatus.NO_SHOW)
    assert res.trip_phase == "Cancelled"
    assert res.is_cancelled is True


def test_trip_status_event_records_change():
    res = TransferReservationFactory()
    event = TripStatusEvent.objects.create(
        reservation=res,
        status=Reservation.TripStatus.ASSIGNED,
        source=TripStatusEvent.Source.MANUAL,
    )
    assert event.reservation == res
    assert str(event)


# --- revenue fields --------------------------------------------------------
def test_reservation_defaults_to_deferred_revenue(db):
    from apps.reservations.factories import TransferReservationFactory

    res = TransferReservationFactory()
    assert res.revenue_status == Reservation.RevenueStatus.DEFERRED
    assert res.recognized_at is None


def test_earned_terminal_statuses():
    assert Reservation.TripStatus.DONE in EARNED_TERMINAL_STATUSES
    assert Reservation.TripStatus.NO_SHOW in EARNED_TERMINAL_STATUSES
    assert Reservation.TripStatus.CANCELLED not in EARNED_TERMINAL_STATUSES


# --- vehicle reference list ------------------------------------------------
def test_vehicle_reference_list_seeded(db):
    from apps.leads.models import VehicleType

    names = set(VehicleType.objects.filter(active=True).values_list("name", flat=True))
    assert {"Luxury Sedan", "Luxury SUV", "Sprinter Van", "Mini Coach", "Motor Coach"} <= names


# --- unified rate x hours + gratuity pricing --------------------------------
def test_line_total_is_rate_times_billed_hours_plus_gratuity():
    r = ReservationFactory(
        trip_type="hourly",
        rate=Decimal("100.00"),
        hours=Decimal("5"),
        min_hours=Decimal("4"),
        gratuity_pct=Decimal("20"),
    )
    assert r.subtotal == Decimal("500.00")  # 100 × override 5
    assert r.gratuity == Decimal("100.00")  # 20% of 500
    assert r.line_total == Decimal("600.00")


def test_hourly_minimum_applies_only_without_an_override():
    r = ReservationFactory(
        trip_type="hourly",
        rate=Decimal("100.00"),
        hours=Decimal("0"),
        min_hours=Decimal("4"),
    )
    assert r.billed_hours == Decimal("4") and r.subtotal == Decimal("400.00")


def test_gratuity_flat_overrides_percent():
    r = ReservationFactory(
        rate=Decimal("100"),
        hours=Decimal("2"),
        min_hours=0,
        gratuity_pct=Decimal("20"),
        gratuity_flat=Decimal("75.00"),
    )
    assert r.gratuity == Decimal("75.00")
    assert r.line_total == Decimal("275.00")  # 200 + 75


def test_zero_gratuity_means_line_total_equals_subtotal():
    r = ReservationFactory(rate=Decimal("100"), hours=Decimal("3"), min_hours=0)
    assert r.gratuity == Decimal("0.00") and r.line_total == r.subtotal


def test_transfer_override_above_the_minimum_bills_the_override():
    res = TransferReservationFactory(
        rate=Decimal("200"), hours=Decimal("2.5"), min_hours=Decimal("1")
    )
    assert res.billed_hours == Decimal("2.5")
    assert res.line_total == Decimal("500.00")
