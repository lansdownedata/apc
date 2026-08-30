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
TODAY = date(2026, 9, 1)  # 0 days out — the only date LIVE_LOOKAHEAD_DAYS=0 routes live
NEAR = date(2026, 9, 3)  # 2 days out — a "gap day": no live coverage on this plan (§3)
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


def test_today_uses_live_flights(iad, united, now, client):
    """Only day 0 has live coverage on this plan (§3: LIVE_LOOKAHEAD_DAYS=0)."""
    future, live = client
    row = _lookup(iad, united, flight_date=TODAY, preferred_time=time(9, 0))
    live.assert_called_once_with(
        airport_iata="IAD",
        direction="arrival",
        date=TODAY,
        airline_iata="UA",
        flight_number="123",
        airport_tz="America/New_York",
        preferred_time=time(9, 0),
    )
    future.assert_not_called()
    assert row.source == Flight.Source.LIVE


def test_boundary_seven_days_is_unavailable_eight_is_future(iad, united, now, client):
    """7 days out is a gap day (no live coverage, no future coverage — flightsFuture hard
    refuses inside 7 days); 8 days out is the first day flightsFuture will answer."""
    future, live = client
    row = _lookup(iad, united, flight_date=date(2026, 9, 8))
    assert row.status == Flight.Status.UNAVAILABLE
    assert future.call_count == 0 and live.call_count == 0
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
        flight_date=TODAY,
        direction="arrival",
        source=Flight.Source.LIVE,
        checked_at=NOW - timedelta(minutes=4),
    )
    _lookup(iad, united, flight_date=TODAY)
    live.assert_not_called()
    Flight.objects.update(checked_at=NOW - timedelta(minutes=6))
    _lookup(iad, united, flight_date=TODAY)
    live.assert_called_once()


def test_a_future_snapshot_that_aged_into_the_gap_window_becomes_unavailable(
    iad, united, now, client
):
    """A future-sourced row whose flight_date has aged inside the live phase (§1's
    LIVE_PHASE_DAYS=7 window) still gets rechecked on the 5-minute cadence — but on this
    plan only day 0 has live coverage (§3), so a gap day (1-7 days out) that gets rechecked
    now comes back UNAVAILABLE, not LIVE, and never calls either provider function."""
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
    future.assert_not_called()
    live.assert_not_called()
    assert row.status == Flight.Status.UNAVAILABLE and row.source == ""


def test_gap_days_are_unavailable_without_a_call(iad, united, now, client):
    future, live = client
    row = _lookup(iad, united, flight_date=NEAR)
    assert row.status == Flight.Status.UNAVAILABLE and row.source == ""
    # NEAR (2 days out) is inside is_live_phase (today + LIVE_PHASE_DAYS=7), and
    # Flight.recheck_window checks is_live_phase *before* status (see
    # test_flight_model.py::test_recheck_windows) — so this row gets the 5-minute live
    # window, not the 1-hour not-found/unavailable window. A "gap day" is, by construction,
    # always inside the live-phase date range (it's the same days_out <= LIVE_PHASE_DAYS
    # band that would route to live_flight if LIVE_LOOKAHEAD_DAYS were still 7), so it can
    # never hit the 1-hour rule.
    assert row.refresh_allowed_at == NOW + timedelta(minutes=5)
    future.assert_not_called()
    live.assert_not_called()
    today_row = _lookup(iad, united, flight_number="7", flight_date=TODAY)
    assert today_row.source == Flight.Source.LIVE  # day-of still goes live


def test_provider_error_propagates_and_caches_nothing(iad, united, now, client):
    future, _ = client
    future.side_effect = av.AviationstackError("rate_limited", "slow down", 429)
    with pytest.raises(av.AviationstackError) as exc:
        _lookup(iad, united)
    assert exc.value.code == "rate_limited"
    assert Flight.objects.count() == 0


def test_refuses_an_airport_with_no_scheduled_service_before_any_call(iad, united, now, client):
    """Andrews (ADW) has a real 3-char IATA code and a timezone — it would sail past the
    other two guards. 391 of the 863 curated airports have no scheduled passenger service
    (military fields, GA relievers); Verify must refuse them without spending a call
    against the rate-limited provider."""
    future, live = client
    iad.has_scheduled_service = False
    iad.save()
    with pytest.raises(flights.FlightLookupError) as exc:
        _lookup(iad, united)
    assert exc.value.code == "no_scheduled_service"
    future.assert_not_called()
    live.assert_not_called()
    assert Flight.objects.count() == 0


@pytest.mark.parametrize(
    "over, code",
    [
        ({"flight_date": date(2026, 8, 31)}, "past_date"),
        ({}, "no_iata"),
        ({}, "no_timezone"),
        ({}, "no_scheduled_service"),
    ],
)
def test_validation_errors(iad, united, now, client, over, code):
    if code == "no_iata":
        iad.iata = "07FA"
        iad.save()
    if code == "no_timezone":
        iad.timezone = ""
        iad.save()
    if code == "no_scheduled_service":
        iad.has_scheduled_service = False
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


# --- other_airport_name is resolved from our own Airport table (final review #1) ---


def test_other_airport_name_is_resolved_from_our_airport_table(iad, united, now):
    """Neither aviationstack endpoint ever sends an airport-name field (aviationstack.py's
    future_schedule/live_flight both leave FlightResult.other_airport_name blank) — lookup
    resolves it itself from addresses.Airport by other_airport_iata, one query, only on the
    API path. A fake IATA code ("ZZ8", not in the real seeded 3,637-row table) keeps this
    deterministic — the real table now has several duplicate codes (see below), so a
    factory row sharing a real code is no longer safe to build a single-result test on."""
    from apps.addresses.factories import AirportFactory as _AirportFactory

    _AirportFactory(iata="ZZ8", name="Denver International")
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="ZZ8",
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == "Denver International"


def test_other_airport_name_falls_back_to_blank_for_an_unknown_iata(iad, united, now):
    """A foreign airport is not in our (US-only) table — no crash, just no name."""
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="ZZZ",
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == ""


# --- 3,637-row expansion introduced 16 duplicate IATA codes; ICAO disambiguates (2026-08-29) ---


def test_other_airport_name_resolves_akr_by_icao_not_the_arbitrary_iata_match(iad, united, now):
    """The real, already-seeded table: AKR is shared by Akron Fulton (curated US row,
    `has_scheduled_service=False`, alphabetically-first name) and Akure, Nigeria (added by
    the global-airport expansion, `has_scheduled_service=True`). With no ordering at all,
    `Airport.objects.filter(iata="AKR").first()` returns Akron — confirmed live against the
    merged code, 2026-08-29 ("flight from Akure, Nigeria (AKR) -> Akron Fulton International
    Airport WRONG"). Passing the flight's real ICAO (Akure's is DNAK) must resolve it right."""
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="AKR",
        other_airport_icao="DNAK",
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == "Akure Airport"


def test_other_airport_name_resolves_saw_to_istanbul_not_marquette(iad, united, now):
    """The real, already-seeded table: SAW is shared by Marquette/Sawyer (curated US row)
    and Istanbul Sabiha Gökçen (global expansion) — both carry `has_scheduled_service=True`,
    so preferring that flag alone (the fix for 9 of the 16 collisions) cannot disambiguate
    this one; ordering ties on a tied `-has_scheduled_service` sort resolve to Marquette,
    the lower-pk curated row (confirmed empirically against this table) — exactly the "loses
    to Marquette/Sawyer" failure mode described in the bug report. Only the globally-unique
    ICAO code can get Istanbul, a genuine JFK long-haul origin, right."""
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="SAW",
        other_airport_icao="LTFJ",
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == "Istanbul Sabiha Gökçen International Airport"


def test_other_airport_name_falls_back_to_iata_preferring_scheduled_service(iad, united, now):
    """No ICAO on the result (the provider sent none, or it doesn't match any row we hold)
    — fall back to IATA, and when that's ambiguous, prefer the row with scheduled service
    (fixes 9 of the 16 collisions per the correctness-bug report). A fake IATA code keeps
    this deterministic (no collision with the real table's own duplicates); the
    non-scheduled row's name sorts first alphabetically, so this only passes if the
    preference is real, not an alphabetical accident."""
    from apps.addresses.factories import AirportFactory as _AirportFactory

    _AirportFactory(iata="QQ1", name="Aaa Non-Scheduled Strip", has_scheduled_service=False)
    _AirportFactory(iata="QQ1", name="Zzz Scheduled Airport", has_scheduled_service=True)
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="QQ1",
        other_airport_icao="",
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == "Zzz Scheduled Airport"


def test_other_airport_name_falls_back_to_iata_when_icao_has_no_match(iad, united, now):
    """The result carries an ICAO code, but it isn't one we hold (e.g. a foreign airport
    outside our table) — degrade to the IATA path rather than coming back blank. Fake codes
    throughout, so a real duplicate can't interfere with the assertion."""
    from apps.addresses.factories import AirportFactory as _AirportFactory

    _AirportFactory(
        iata="QQ2", icao="KQQ2", name="Test Junction Airport", has_scheduled_service=True
    )
    found = av.FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        other_airport_iata="QQ2",
        other_airport_icao="ZZZZ",  # no row on file has this ICAO
    )
    with patch.object(flights.aviationstack, "future_schedule", return_value=found):
        row = _lookup(iad, united)
    assert row.other_airport_name == "Test Junction Airport"


# --- a day-of refresh must not erase richer detail timetable doesn't send (review #2) ---


def test_live_refresh_does_not_erase_terminal_gate_and_other_airport(iad, united, now):
    """timetable often sends null terminal/gate (2 of 3 rows in the captured sample) and
    never an airport-name field — a blank value on a refresh means "not reported," never
    "erase." Seed a future-phase row carrying the richer detail, refresh it through the live
    path with a thin (blank-everything-but-schedule) result, and confirm the dispatcher-facing
    detail survives instead of being wiped."""
    FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=TODAY,
        direction="arrival",
        source=Flight.Source.FUTURE,
        checked_at=NOW - timedelta(hours=25),
        terminal="C",
        gate="C7",
        other_airport_iata="DEN",
        other_airport_name="Denver International",
    )
    thin = av.FlightResult(
        found=True, status="scheduled", scheduled_at=datetime(2026, 9, 1, 21, 35, tzinfo=UTC)
    )
    with patch.object(flights.aviationstack, "live_flight", return_value=thin):
        row = _lookup(iad, united, flight_date=TODAY)
    assert row.source == Flight.Source.LIVE
    assert (row.terminal, row.gate) == ("C", "C7")
    assert row.other_airport_iata == "DEN"
    assert row.other_airport_name == "Denver International"
