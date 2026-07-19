from decimal import Decimal

import pytest

from apps.reservations.factories import (
    HourlyReservationFactory,
    StopFactory,
    TransferReservationFactory,
)
from apps.reservations.models import EARNED_TERMINAL_STATUSES, Reservation, TripStatusEvent

pytestmark = pytest.mark.django_db


# --- pricing ---------------------------------------------------------------
def test_transfer_line_total_is_base_rate():
    res = TransferReservationFactory(base_rate=Decimal("185"))
    assert res.line_total == Decimal("185.00")


def test_hourly_line_total_is_hours_times_rate():
    res = HourlyReservationFactory(
        hours=Decimal("5"), hourly_rate=Decimal("240"), min_hours=Decimal("4")
    )
    assert res.billed_hours == Decimal("5")
    assert res.min_applied is False
    assert res.line_total == Decimal("1200.00")


def test_hourly_applies_minimum_when_below():
    res = HourlyReservationFactory(
        hours=Decimal("2"), hourly_rate=Decimal("300"), min_hours=Decimal("4")
    )
    assert res.billed_hours == Decimal("4")
    assert res.min_applied is True
    assert res.line_total == Decimal("1200.00")


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
