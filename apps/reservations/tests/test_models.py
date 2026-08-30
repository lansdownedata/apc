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


# --- flight info on an airport stop ----------------------------------------
def _airport_stop(**kwargs):
    from apps.addresses.factories import AirlineFactory, AirportFactory

    res = ReservationFactory(stops=[])
    kwargs.setdefault("airport", AirportFactory(iata="IAD"))
    if "airline" not in kwargs:
        kwargs["airline"] = AirlineFactory(iata="UA", name="United Airlines")
    return StopFactory(reservation=res, sequence=0, address="Dulles", **kwargs)


def test_flight_label_is_code_and_number():
    stop = _airport_stop(flight_number="123")
    assert stop.flight_label == "UA 123"
    assert stop.flight_label_long == "United Airlines 123"


def test_flight_label_falls_back_to_the_airline_alone():
    stop = _airport_stop(flight_number="")
    assert stop.flight_label == "United Airlines"
    assert stop.flight_label_long == "United Airlines"


def test_flight_label_with_only_a_number_says_flight():
    stop = _airport_stop(airline=None, flight_number="123")
    assert stop.flight_label == "Flight 123"
    assert stop.flight_label_long == "Flight 123"


def test_flight_label_is_blank_without_flight_info():
    stop = _airport_stop(airline=None, flight_number="")
    assert stop.flight_label == ""
    assert stop.flight_label_long == ""


def test_ordered_stops_join_the_airline_and_airport(django_assert_num_queries):
    stop = _airport_stop(flight_number="123")
    res = Reservation.objects.get(pk=stop.reservation_id)
    with django_assert_num_queries(1):
        labels = [s.flight_label for s in res.ordered_stops]
    assert labels == ["UA 123"]


def test_flight_factory_defaults(db):
    from apps.reservations.factories import FlightFactory
    from apps.reservations.models import Flight

    f = FlightFactory()
    assert f.status == Flight.Status.SCHEDULED and f.source == Flight.Source.FUTURE
    assert f.checked_at is not None and f.scheduled_at > f.checked_at
    assert f.airport.timezone == "America/New_York"


# --- Reservation.flight_summary (spec 2026-08-29 §4.4) ---


def _airport_trip(states):
    """A reservation whose stops carry flights in the given pill states ("" = no flight,
    "unverified" = flight info but no cache row)."""
    from datetime import UTC, date, datetime

    from apps.addresses.factories import AirlineFactory, AirportFactory
    from apps.reservations.factories import FlightFactory, ReservationFactory
    from apps.reservations.models import Flight

    airport = AirportFactory(iata="IAD")
    airline = AirlineFactory(iata="UA")
    res = ReservationFactory(
        stops=[f"S{i}" for i in range(len(states))], pickup_date=date(2026, 9, 2)
    )
    status_for = {
        "verified": (Flight.Status.SCHEDULED, Flight.Source.FUTURE, None),
        "on_time": (Flight.Status.ACTIVE, Flight.Source.LIVE, 0),
        "delayed": (Flight.Status.ACTIVE, Flight.Source.LIVE, 40),
        "cancelled": (Flight.Status.CANCELLED, Flight.Source.LIVE, None),
        "not_found": (Flight.Status.NOT_FOUND, Flight.Source.FUTURE, None),
        "unavailable": (Flight.Status.UNAVAILABLE, "", None),
    }
    for stop, state in zip(res.stops.order_by("sequence"), states, strict=True):
        if not state:
            continue
        stop.airport, stop.airline, stop.flight_number = airport, airline, str(stop.sequence)
        stop.flight_direction = "arrival"
        if state != "unverified":
            status, source, delay = status_for[state]
            stop.flight = FlightFactory(
                airline=airline,
                airport=airport,
                flight_number=str(stop.sequence),
                flight_date=date(2026, 9, 2),
                direction="arrival",
                status=status,
                source=source,
                delay_minutes=delay,
                scheduled_at=datetime(2026, 9, 2, 21, 35, tzinfo=UTC),
            )
        stop.save()
    return res


@pytest.mark.parametrize(
    "states, expected_state, expected_label",
    [
        (["", ""], None, None),
        (["unverified", ""], "unverified", "Verify flight"),
        (["unverified", "unverified"], "unverified", "Verify flights"),
        (["verified", ""], "verified", "Flight verified"),
        (["verified", "on_time"], "verified", "Flights verified"),
        (["verified", "unverified"], "partial", "1 of 2 verified"),
        (["verified", "unavailable"], "partial", "1 of 2 verified"),
        (["delayed", "verified"], "delayed", "1 flight delayed"),
        (["delayed", "delayed"], "delayed", "2 flights delayed"),
        (["not_found", "verified"], "not_found", "Flight not found"),
        # Pins the precedence order itself: delayed must outrank not_found (final review
        # #7a — swapping the two in the precedence tuple left this whole file green).
        (["not_found", "delayed"], "delayed", "1 flight delayed"),
        (["cancelled", "delayed", "not_found"], "cancelled", "Flight cancelled"),
    ],
)
def test_flight_summary_rolls_up_worst_state_first(db, states, expected_state, expected_label):
    from apps.reservations.models import Reservation

    res = Reservation.objects.prefetch_related(
        "stops__flight__airport", "stops__flight__airline"
    ).get(pk=_airport_trip(states).pk)
    summary = res.flight_summary
    if expected_state is None:
        assert summary is None
    else:
        assert summary["state"] == expected_state
        assert summary["label"] == expected_label
        assert summary["chip"] and summary["icon"]


def test_flight_summary_uses_the_prefetch_not_ordered_stops(db, django_assert_num_queries):
    from apps.reservations.models import Reservation

    pk = _airport_trip(["verified", "delayed", "verified"]).pk
    res = Reservation.objects.prefetch_related(
        "stops__flight__airport", "stops__flight__airline"
    ).get(pk=pk)
    with django_assert_num_queries(0):
        assert res.flight_summary["state"] == "delayed"


def test_ordered_stops_joins_the_flight(db, django_assert_num_queries):
    res = _airport_trip(["verified", ""])
    stops = list(res.ordered_stops)
    with django_assert_num_queries(0):
        assert stops[0].flight.pill()["state"] == "verified"
