"""The shared trip sheet — `flight_line` / `trip_sheet_context` / `trip_sheet_text`.

The flight detail here is what an affiliate meeting a plane actually needs (time,
terminal), matching what the offer email has carried since the flight-verification work.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import FlightFactory, ReservationFactory, StopFactory
from apps.reservations.models import Flight, FlightDirection
from apps.reservations.services import flight_line, trip_sheet_context, trip_sheet_text

pytestmark = pytest.mark.django_db


def _trip(**kw):
    kw.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kw.setdefault("pickup_date", (timezone.now() + timedelta(days=20)).date())
    return ReservationFactory(**kw)


# --- flight_line -------------------------------------------------------------------


def test_a_stop_with_no_flight_has_no_line():
    stop = StopFactory(reservation=_trip(), address="Somewhere")
    assert flight_line(stop) == ""


def test_an_unverified_flight_shows_just_its_label():
    airline = AirlineFactory(iata="UA")
    stop = StopFactory(
        reservation=_trip(),
        address="Dulles",
        airport=AirportFactory(),
        airline=airline,
        flight_number="123",
        flight_direction=FlightDirection.ARRIVAL,
    )
    assert flight_line(stop) == "UA 123"


def test_a_verified_arrival_shows_time_zone_and_terminal():
    airline = AirlineFactory(iata="UA")
    airport = AirportFactory(timezone="America/New_York")
    flight = FlightFactory(
        airline=airline,
        airport=airport,
        flight_number="123",
        direction=FlightDirection.ARRIVAL,
        terminal="C",
    )
    stop = StopFactory(
        reservation=_trip(),
        address="Dulles",
        airport=airport,
        airline=airline,
        flight_number="123",
        flight_direction=FlightDirection.ARRIVAL,
        flight=flight,
    )

    line = flight_line(stop)

    assert line.startswith("UA 123 · arr ")
    assert "Terminal C" in line
    assert line.endswith(("EDT", "EST", "Terminal C"))


def test_a_departure_says_dep():
    airline = AirlineFactory(iata="DL")
    airport = AirportFactory(timezone="America/New_York")
    flight = FlightFactory(
        airline=airline, airport=airport, flight_number="99", direction=FlightDirection.DEPARTURE
    )
    stop = StopFactory(
        reservation=_trip(),
        address="Reagan National",
        airport=airport,
        airline=airline,
        flight_number="99",
        flight_direction=FlightDirection.DEPARTURE,
        flight=flight,
    )

    assert " · dep " in flight_line(stop)


@pytest.mark.parametrize(
    ("status", "word"),
    [(Flight.Status.CANCELLED, "Cancelled"), (Flight.Status.DIVERTED, "Diverted")],
)
def test_a_cancelled_flight_says_so_instead_of_a_time(status, word):
    """Its scheduled time survives the cancellation — printing it would send an affiliate
    to meet a plane that isn't coming."""
    airline = AirlineFactory(iata="UA")
    airport = AirportFactory(timezone="America/New_York")
    flight = FlightFactory(
        airline=airline,
        airport=airport,
        flight_number="123",
        direction=FlightDirection.ARRIVAL,
        terminal="C",
        status=status,
    )
    stop = StopFactory(
        reservation=_trip(),
        address="Dulles",
        airport=airport,
        airline=airline,
        flight_number="123",
        flight_direction=FlightDirection.ARRIVAL,
        flight=flight,
    )

    assert flight_line(stop) == f"UA 123 · {word}"


# --- the sheet ---------------------------------------------------------------------


def test_context_carries_one_flight_line_per_flight_stop():
    trip = _trip(stops=["Dulles", "The Ritz"])
    airline = AirlineFactory(iata="AA")
    stop = trip.stops.order_by("sequence").first()
    stop.airport = AirportFactory()
    stop.airline = airline
    stop.flight_number = "456"
    stop.flight_direction = FlightDirection.ARRIVAL
    stop.save()

    ctx = trip_sheet_context(trip)

    assert ctx["flight_lines"] == ["AA 456"]


def test_text_sheet_includes_the_flight_line():
    trip = _trip(stops=["Dulles", "The Ritz"])
    airline = AirlineFactory(iata="AA")
    stop = trip.stops.order_by("sequence").first()
    stop.airport = AirportFactory()
    stop.airline = airline
    stop.flight_number = "456"
    stop.flight_direction = FlightDirection.ARRIVAL
    stop.save()

    assert "Flight: AA 456" in trip_sheet_text(trip)


def test_a_flightless_trip_has_no_flight_lines():
    trip = _trip(stops=["A", "B"])
    assert trip_sheet_context(trip)["flight_lines"] == []
    assert "Flight:" not in trip_sheet_text(trip)
