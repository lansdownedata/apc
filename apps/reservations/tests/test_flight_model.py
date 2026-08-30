"""reservations.Flight — the aviationstack cache row and the one pill it renders."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.reservations.factories import FlightFactory, ReservationFactory, StopFactory
from apps.reservations.models import Flight, FlightDirection, Stop, today_at

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)  # 11:00 EDT


@pytest.fixture
def iad():
    return AirportFactory(iata="IAD", name="Washington Dulles Intl", timezone="America/New_York")


@pytest.fixture
def united():
    return AirlineFactory(iata="UA", name="United Airlines")


@pytest.fixture
def lax():
    return AirportFactory(iata="LAX", name="Los Angeles Intl", timezone="America/Los_Angeles")


def _flight(iad, united, **over):
    kwargs = dict(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=date(2026, 10, 15),
        direction=FlightDirection.ARRIVAL,
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),  # 5:35 PM EDT
        checked_at=NOW,
        source=Flight.Source.FUTURE,
        other_airport_iata="DEN",
        other_airport_name="Denver International",
        terminal="C",
    )
    kwargs.update(over)
    return FlightFactory(**kwargs)


def test_today_at_uses_the_airport_zone():
    with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 2, 3, 0, tzinfo=UTC)):
        assert today_at("America/New_York") == date(2026, 9, 1)  # 11 PM EDT the day before
        assert today_at("Europe/London") == date(2026, 9, 2)
        assert today_at("") == timezone.localdate()


def test_str_and_uniqueness(iad, united):
    f = _flight(iad, united)
    assert str(f) == "UA 123 · IAD arrival · 2026-10-15"
    with pytest.raises(IntegrityError):
        _flight(iad, united)


def test_phase_boundary_is_seven_days_in_the_airport_zone(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        assert _flight(iad, united, flight_date=date(2026, 9, 8)).is_live_phase  # +7
        assert not _flight(iad, united, flight_date=date(2026, 9, 9)).is_live_phase  # +8


def test_recheck_windows(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        future = _flight(iad, united)
        assert future.recheck_window == timedelta(hours=24)
        assert future.refresh_allowed_at == NOW + timedelta(hours=24)
        assert _flight(
            iad, united, flight_number="9", status=Flight.Status.NOT_FOUND
        ).recheck_window == timedelta(hours=1)
        assert _flight(
            iad, united, flight_number="8", status=Flight.Status.UNAVAILABLE
        ).recheck_window == timedelta(hours=1)
        live = _flight(iad, united, flight_number="7", flight_date=date(2026, 9, 2))
        assert live.recheck_window == timedelta(minutes=5)


def test_best_at_and_effective_delay(iad, united):
    f = _flight(iad, united, estimated_at=datetime(2026, 10, 15, 22, 15, tzinfo=UTC))
    assert f.best_at == f.estimated_at
    assert f.effective_delay == 40
    f.delay_minutes = 12
    assert f.effective_delay == 12
    early = _flight(
        iad, united, flight_number="5", estimated_at=datetime(2026, 10, 15, 21, 20, tzinfo=UTC)
    )
    assert early.effective_delay == 0


def test_times_render_in_the_airport_zone_with_abbreviation(iad, united):
    f = _flight(iad, united)
    assert f.time_local == "5:35 PM"
    assert f.tz_abbr == "EDT"
    f.flight_date = date(2026, 12, 10)
    f.scheduled_at = datetime(2026, 12, 10, 22, 35, tzinfo=UTC)
    assert (f.time_local, f.tz_abbr) == ("5:35 PM", "EST")


def test_pill_state_by_status_and_source(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        assert _flight(iad, united).pill_state == "verified"
        live = dict(
            flight_date=date(2026, 9, 2),
            source=Flight.Source.LIVE,
            scheduled_at=datetime(2026, 9, 2, 21, 35, tzinfo=UTC),
        )
        assert _flight(iad, united, flight_number="1", **live).pill_state == "on_time"
        assert (
            _flight(iad, united, flight_number="2", delay_minutes=11, **live).pill_state
            == "delayed"
        )
        assert (
            _flight(iad, united, flight_number="3", delay_minutes=10, **live).pill_state
            == "on_time"
        )
        assert (
            _flight(iad, united, flight_number="4", status=Flight.Status.LANDED, **live).pill_state
            == "landed"
        )
        assert (
            _flight(
                iad, united, flight_number="5", status=Flight.Status.CANCELLED, **live
            ).pill_state
            == "cancelled"
        )
        assert (
            _flight(
                iad, united, flight_number="6", status=Flight.Status.DIVERTED, **live
            ).pill_state
            == "cancelled"
        )
        assert (
            _flight(iad, united, flight_number="7", status=Flight.Status.NOT_FOUND).pill_state
            == "not_found"
        )
        assert (
            _flight(iad, united, flight_number="8", status=Flight.Status.UNAVAILABLE).pill_state
            == "unavailable"
        )
        # A future snapshot that aged into the live window still reads as "verified", never
        # "on time" — it has no live data until someone refreshes it.
        aged = _flight(
            iad,
            united,
            flight_number="9",
            flight_date=date(2026, 9, 2),
            scheduled_at=datetime(2026, 9, 2, 21, 35, tzinfo=UTC),
        )
        assert aged.pill_state == "verified"


def test_pill_verified_future(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(iad, united).pill()
    assert p["state"] == "verified" and p["chip"] == "chip-ok" and p["icon"] == "ti-plane-arrival"
    assert p["label"] == "UA 123 · 5:35 PM EDT"
    assert p["label_compact"] == "UA 123 · 5:35 PM"
    assert p["detail"] == (
        "Arrives 5:35 PM EDT · Terminal C · from Denver International (DEN) · checked Sep 1"
    )
    assert p["refresh_allowed_at"] == (NOW + timedelta(hours=24)).isoformat()
    assert p["other_airport"] == "Denver International (DEN)"


def test_pill_checked_date_uses_the_airport_zone_not_the_server(lax, united):
    """The "checked" date in a verified pill must come from the airport's own zone
    (self.local()), never settings.TIME_ZONE (America/New_York here). 05:30 UTC on
    2026-09-02 is Sep 2 in Eastern but still Sep 1 in Pacific — pick a non-Eastern airport
    (LAX) and a checked_at that straddles that boundary to catch a server-zone regression."""
    checked_at = datetime(2026, 9, 2, 5, 30, tzinfo=UTC)
    with patch("django.utils.timezone.now", return_value=checked_at):
        p = _flight(lax, united, checked_at=checked_at).pill()
    assert p["detail"] == (
        "Arrives 2:35 PM PDT · Terminal C · from Denver International (DEN) · checked Sep 1"
    )


def test_pill_other_airport_renders_bare_code_when_name_is_blank(iad, united):
    """Neither aviationstack endpoint sends an airport-name field, so a row whose far-end
    IATA code isn't in our own Airport table (final review #1) has a blank
    `other_airport_name` — the label must fall back to the bare code cleanly, never
    'from  (DEN)' or a stray trailing separator."""
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(iad, united, other_airport_name="").pill()
    assert p["other_airport"] == "DEN"
    assert "from DEN" in p["detail"]
    assert "(DEN)" not in p["detail"]


def test_pill_guards_a_found_flight_with_no_parseable_time(iad, united):
    """found=True but scheduled/estimated/actual all blank (a malformed provider time) must
    not leave a trailing ' · ' in the label or a double space in the detail (final review
    #8's pill() guard)."""
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(iad, united, scheduled_at=None).pill()
    assert p["label"] == "UA 123"
    assert "  " not in p["detail"]
    assert p["detail"] == ("Arrives · Terminal C · from Denver International (DEN) · checked Sep 1")


def test_pill_delayed_live(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(
            iad,
            united,
            flight_date=date(2026, 9, 2),
            source=Flight.Source.LIVE,
            status=Flight.Status.ACTIVE,
            gate="C7",
            scheduled_at=datetime(2026, 9, 2, 21, 35, tzinfo=UTC),
            estimated_at=datetime(2026, 9, 2, 22, 15, tzinfo=UTC),
            checked_at=NOW - timedelta(minutes=3),
        ).pill()
    assert p["state"] == "delayed" and p["chip"] == "chip-warn"
    assert p["icon"] == "ti-clock-exclamation"
    assert p["label"] == "UA 123 · +40m · 6:15 PM EDT"
    assert p["scheduled_local"] == "5:35 PM"
    assert p["detail"].startswith("Arrives 6:15 PM EDT · scheduled 5:35 PM · Terminal C · Gate C7")
    assert p["detail"].endswith("from Denver International (DEN) · updated 3 minutes ago")
    assert p["checked_ago"] == "3 minutes"


def test_pill_departure_landed_reads_departed(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(
            iad,
            united,
            direction=FlightDirection.DEPARTURE,
            flight_date=date(2026, 9, 2),
            source=Flight.Source.LIVE,
            status=Flight.Status.ACTIVE,
            scheduled_at=datetime(2026, 9, 2, 11, 15, tzinfo=UTC),
            actual_at=datetime(2026, 9, 2, 11, 12, tzinfo=UTC),
        ).pill()
    assert p["state"] == "landed" and p["icon"] == "ti-plane-departure"
    assert p["label"] == "UA 123 · Departed 7:12 AM EDT"
    assert "to Denver International (DEN)" in p["detail"]


def test_pill_cancelled(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(
            iad,
            united,
            flight_date=date(2026, 9, 2),
            source=Flight.Source.LIVE,
            status=Flight.Status.CANCELLED,
            scheduled_at=datetime(2026, 9, 2, 21, 35, tzinfo=UTC),
        ).pill()
    assert p["chip"] == "chip-danger" and p["icon"] == "ti-plane-off"
    assert p["label"] == "UA 123 · Cancelled"
    assert p["detail"].startswith("was arriving 5:35 PM EDT · from Denver International (DEN)")


def test_pill_not_found_copy_differs_by_phase(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        future = _flight(iad, united, status=Flight.Status.NOT_FOUND, scheduled_at=None).pill()
        live = _flight(
            iad,
            united,
            flight_number="7",
            flight_date=date(2026, 9, 2),
            status=Flight.Status.NOT_FOUND,
            scheduled_at=None,
        ).pill()
    assert future["label"] == "UA 123 · Not found" and future["icon"] == "ti-help-circle"
    assert future["detail"] == (
        "No UA 123 arriving at IAD on Oct 15 — not found, or not published yet. "
        "Check the number, or the flight may use another airport."
    )
    assert live["detail"] == (
        "No UA 7 arriving at IAD on Sep 2. Check the number, or the flight may use another airport."
    )


def test_pill_unavailable_and_operated_by(iad, united):
    with patch("django.utils.timezone.now", return_value=NOW):
        p = _flight(iad, united, status=Flight.Status.UNAVAILABLE, scheduled_at=None).pill()
        op = _flight(
            iad, united, flight_number="7601", operated_by_iata="LH", operated_by_name="Lufthansa"
        ).pill()
    assert p["label"] == "UA 123 · Live on the day" and p["chip"] == "chip-ring"
    assert p["detail"] == "Live data available on the day of travel"
    assert op["detail"].endswith(" · Operated by Lufthansa")


def test_stop_fields_default_blank_and_link_is_nullable(iad, united):
    stop = StopFactory(
        reservation=ReservationFactory(), airport=iad, airline=united, flight_number="123"
    )
    assert stop.flight_direction == "" and stop.flight is None and stop.flight_pill is None
    stop.flight = _flight(iad, united)
    stop.flight_direction = Stop.FlightDirection.ARRIVAL
    stop.save()
    assert Stop.objects.get(pk=stop.pk).flight_pill["state"] == "verified"


def test_deleting_a_flight_row_unlinks_stops_rather_than_cascading(iad, united):
    f = _flight(iad, united)
    stop = StopFactory(
        reservation=ReservationFactory(), airport=iad, airline=united, flight_number="123", flight=f
    )
    f.delete()
    stop.refresh_from_db()
    assert stop.flight is None


def test_migration_backfills_direction_by_position(iad, united):
    """Existing airport stops get arrival/departure from their position (spec §4.2). Verified
    here through the same rule the migration uses, applied to freshly created rows."""
    from apps.reservations.migrations import backfill_flight_direction

    res = ReservationFactory(stops=["IAD", "Hotel", "DCA"])
    for stop in res.stops.all():
        if stop.address in ("IAD", "DCA"):
            stop.airport = iad
            stop.save()
    backfill_flight_direction(Reservation=type(res), Stop=Stop)
    by_seq = {s.sequence: s.flight_direction for s in res.stops.all()}
    assert by_seq == {0: "arrival", 1: "", 2: "departure"}
