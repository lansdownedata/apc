"""flights.lookup — cache first, phase by date, re-check windows, nothing cached on error."""

from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.integrations import aviationstack as av
from apps.reservations import flights
from apps.reservations.factories import FlightFactory
from apps.reservations.models import Flight

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)  # Sep 1, 11:00 EDT
FAR = date(2026, 10, 15)
NEAR = date(2026, 9, 3)
FOUND = av.FlightResult(
    found=True,
    status="scheduled",
    scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
    terminal="C",
    other_airport_iata="DEN",
    raw={"flight": {"iataNumber": "UA123"}},
)


@pytest.fixture
def iad():
    return AirportFactory(iata="IAD", timezone="America/New_York")


@pytest.fixture
def united():
    return AirlineFactory(iata="UA", name="United Airlines")


@pytest.fixture
def now():
    with patch("django.utils.timezone.now", return_value=NOW):
        yield NOW


@pytest.fixture
def client():
    with (
        patch.object(flights.aviationstack, "future_schedule", return_value=FOUND) as future,
        patch.object(flights.aviationstack, "live_flight", return_value=FOUND) as live,
    ):
        yield future, live


def _lookup(iad, united, **over):
    kwargs = dict(
        airline=united, flight_number="123", airport=iad, direction="arrival", flight_date=FAR
    )
    kwargs.update(over)
    return flights.lookup(**kwargs)


def test_far_date_uses_flights_future_and_caches(iad, united, now, client):
    future, live = client
    row = _lookup(iad, united, preferred_time=time(17, 0))
    future.assert_called_once_with(
        airport_iata="IAD",
        direction="arrival",
        date=FAR,
        airline_iata="UA",
        flight_number="123",
        airport_tz="America/New_York",
        preferred_time=time(17, 0),
    )
    live.assert_not_called()
    assert row.pk and row.status == Flight.Status.SCHEDULED
    assert row.source == Flight.Source.FUTURE and row.checked_at == NOW
    assert row.scheduled_at == FOUND.scheduled_at and row.terminal == "C"
    assert row.raw == FOUND.raw
    assert Flight.objects.count() == 1


def test_near_date_uses_live_flights(iad, united, now, client):
    future, live = client
    row = _lookup(iad, united, flight_date=NEAR)
    live.assert_called_once()
    future.assert_not_called()
    assert row.source == Flight.Source.LIVE


def test_boundary_seven_days_is_live_eight_is_future(iad, united, now, client):
    future, live = client
    _lookup(iad, united, flight_date=date(2026, 9, 8))
    assert live.call_count == 1 and future.call_count == 0
    _lookup(iad, united, flight_date=date(2026, 9, 9))
    assert future.call_count == 1


def test_cache_hit_inside_the_window_makes_no_call(iad, united, now, client):
    future, _ = client
    FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=FAR,
        direction="arrival",
        checked_at=NOW - timedelta(hours=23),
    )
    _lookup(iad, united)
    future.assert_not_called()


def test_future_row_refetches_after_24h(iad, united, now, client):
    future, _ = client
    FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=FAR,
        direction="arrival",
        checked_at=NOW - timedelta(hours=25),
        terminal="old",
    )
    row = _lookup(iad, united)
    future.assert_called_once()
    assert row.terminal == "C" and row.checked_at == NOW
    assert Flight.objects.count() == 1  # updated in place, never a duplicate


def test_not_found_is_cached_with_a_one_hour_window(iad, united, now, client):
    future, _ = client
    future.return_value = av.NOT_FOUND
    row = _lookup(iad, united)
    assert row.status == Flight.Status.NOT_FOUND and row.scheduled_at is None
    assert row.refresh_allowed_at == NOW + timedelta(hours=1)
    _lookup(iad, united)
    assert future.call_count == 1


def test_live_row_refetches_only_after_five_minutes(iad, united, now, client):
    _, live = client
    FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=NEAR,
        direction="arrival",
        source=Flight.Source.LIVE,
        checked_at=NOW - timedelta(minutes=4),
    )
    _lookup(iad, united, flight_date=NEAR)
    live.assert_not_called()
    Flight.objects.update(checked_at=NOW - timedelta(minutes=6))
    _lookup(iad, united, flight_date=NEAR)
    live.assert_called_once()


def test_a_future_snapshot_that_aged_into_the_live_window_refreshes_live(iad, united, now, client):
    future, live = client
    FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=NEAR,
        direction="arrival",
        source=Flight.Source.FUTURE,
        checked_at=NOW - timedelta(days=20),
    )
    row = _lookup(iad, united, flight_date=NEAR)
    live.assert_called_once()
    future.assert_not_called()
    assert row.source == Flight.Source.LIVE


def test_gap_days_are_unavailable_without_a_call_when_lookahead_is_zero(
    iad, united, now, client, monkeypatch
):
    future, live = client
    monkeypatch.setattr(flights, "LIVE_LOOKAHEAD_DAYS", 0)
    row = _lookup(iad, united, flight_date=NEAR)
    assert row.status == Flight.Status.UNAVAILABLE and row.source == ""
    # NEAR (2 days out) is inside is_live_phase (today + LIVE_PHASE_DAYS=7), and
    # Flight.recheck_window checks is_live_phase *before* status (see
    # test_flight_model.py::test_recheck_windows) — so this row gets the 5-minute live
    # window, not the 1-hour not-found/unavailable window. A "gap day" is, by construction,
    # always inside the live-phase date range (it's the same days_out <= LIVE_PHASE_DAYS
    # band that would normally route to live_flight), so it can never hit the 1-hour rule.
    assert row.refresh_allowed_at == NOW + timedelta(minutes=5)
    future.assert_not_called()
    live.assert_not_called()
    today_row = _lookup(iad, united, flight_number="7", flight_date=date(2026, 9, 1))
    assert today_row.source == Flight.Source.LIVE  # day-of still goes live


def test_provider_error_propagates_and_caches_nothing(iad, united, now, client):
    future, _ = client
    future.side_effect = av.AviationstackError("rate_limited", "slow down", 429)
    with pytest.raises(av.AviationstackError) as exc:
        _lookup(iad, united)
    assert exc.value.code == "rate_limited"
    assert Flight.objects.count() == 0


@pytest.mark.parametrize(
    "over, code",
    [
        ({"flight_date": date(2026, 8, 31)}, "past_date"),
        ({}, "no_iata"),
        ({}, "no_timezone"),
    ],
)
def test_validation_errors(iad, united, now, client, over, code):
    if code == "no_iata":
        iad.iata = "07FA"
        iad.save()
    if code == "no_timezone":
        iad.timezone = ""
        iad.save()
    with pytest.raises(flights.FlightLookupError) as exc:
        _lookup(iad, united, **over)
    assert exc.value.code == code
    assert Flight.objects.count() == 0


def test_today_is_counted_in_the_airport_zone(united, client):
    """At 03:00 UTC on Sep 2 it is still Sep 1 in Washington: a Sep 1 flight is not past."""
    airport = AirportFactory(iata="IAD", timezone="America/New_York")
    with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 2, 3, 0, tzinfo=UTC)):
        row = _lookup(airport, united, flight_date=date(2026, 9, 1))
    assert row.source == Flight.Source.LIVE
