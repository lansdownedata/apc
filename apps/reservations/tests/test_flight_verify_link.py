"""The drawer's Verify posts a `stop` id along with the flight it's checking — the one save
path the drawer has for its own link, since it has no editor to fall back on (dispatch gap
fix, 2026-08-29). The editor's payload never carries `stop`; that path must stay a pure
lookup with no writes.
"""

import json
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.dispatch import services as dispatch_services
from apps.leads.models import Lead
from apps.reservations import views
from apps.reservations.factories import FlightFactory, ReservationFactory
from apps.reservations.models import FlightDirection
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
TRIP_DATE = date(2026, 10, 15)


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def iad():
    return AirportFactory(iata="IAD", timezone="America/New_York")


@pytest.fixture
def united():
    return AirlineFactory(iata="UA", name="United Airlines")


def _post(client, payload):
    return client.post(
        reverse("flight_verify"), data=json.dumps(payload), content_type="application/json"
    )


def _trip_with_airport_stop(iad, united, **stop_over):
    """A trip with one airport stop (UA 123, arrival, TRIP_DATE) and one plain stop."""
    res = ReservationFactory(pickup_date=TRIP_DATE)
    stop = res.stops.first()
    stop.airport = iad
    stop.airline = united
    stop.flight_number = "123"
    stop.flight_direction = FlightDirection.ARRIVAL
    for field, value in stop_over.items():
        setattr(stop, field, value)
    stop.save()
    return res, stop


def _body(iad, united, **over):
    base = {
        "airport": iad.pk,
        "airline": united.pk,
        "flight": "123",
        "date": TRIP_DATE.isoformat(),
        "direction": "arrival",
        "time": "16:45",
    }
    base.update(over)
    return base


def _verified_row(iad, united):
    return FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=TRIP_DATE,
        direction="arrival",
    )


def test_without_a_stop_id_writes_nothing(staff, iad, united):
    """The editor's path — no `stop` key — must not touch any Stop row, exactly as before
    this fix."""
    res, stop = _trip_with_airport_stop(iad, united)
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        resp = _post(staff, _body(iad, united))
    assert resp.status_code == 200
    stop.refresh_from_db()
    assert stop.flight_id is None


def test_a_matching_stop_id_links_it(staff, iad, united):
    res, stop = _trip_with_airport_stop(iad, united)
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        resp = _post(staff, _body(iad, united, stop=stop.pk))
    assert resp.status_code == 200
    stop.refresh_from_db()
    assert stop.flight_id == row.pk


@pytest.mark.parametrize(
    "stop_over",
    [
        pytest.param({"flight_number": "999"}, id="wrong-flight-number"),
        pytest.param({"flight_direction": FlightDirection.DEPARTURE}, id="wrong-direction"),
    ],
)
def test_a_mismatched_stop_is_not_linked(staff, iad, united, stop_over):
    res, stop = _trip_with_airport_stop(iad, united, **stop_over)
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        resp = _post(staff, _body(iad, united, stop=stop.pk))
    assert resp.status_code == 200
    assert resp.json()["state"] == "verified"
    stop.refresh_from_db()
    assert stop.flight_id is None


def test_a_stale_trip_date_is_not_linked(staff, iad, united):
    """The trip moved after the drawer was opened — the stop still says UA 123 arrival, but
    the reservation's pickup_date no longer matches the flight just verified."""
    res, stop = _trip_with_airport_stop(iad, united)
    res.pickup_date = date(2026, 10, 16)
    res.save(update_fields=["pickup_date"])
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        resp = _post(staff, _body(iad, united, stop=stop.pk))
    assert resp.status_code == 200
    stop.refresh_from_db()
    assert stop.flight_id is None


def test_a_nonexistent_stop_id_does_not_crash(staff, iad, united):
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        resp = _post(staff, _body(iad, united, stop=999999))
    assert resp.status_code == 200
    assert resp.json()["state"] == "verified"


def test_linking_flips_the_trips_flight_summary(staff, iad, united):
    res, stop = _trip_with_airport_stop(iad, united)
    assert res.flight_summary["state"] == "unverified"
    row = _verified_row(iad, united)
    with patch.object(views.flights, "lookup", return_value=row):
        _post(staff, _body(iad, united, stop=stop.pk))
    assert res.flight_summary["state"] == "verified"


def test_linking_makes_the_trip_sheet_email_carry_the_flight_time(staff, iad, united, mailoutbox):
    """The affiliate trip-sheet email gates its flight line on `stop.flight_id` — before this
    fix the drawer's Verify never set it, so a dispatcher-verified flight never reached the
    affiliate."""
    res, stop = _trip_with_airport_stop(iad, united)
    res.lead.status = Lead.Status.BOOKED
    res.lead.save(update_fields=["status"])
    row = _verified_row(iad, united)
    row.terminal = "B"
    row.scheduled_at = datetime(2026, 10, 15, 18, 35, tzinfo=UTC)
    row.save(update_fields=["terminal", "scheduled_at"])
    with patch.object(views.flights, "lookup", return_value=row):
        _post(staff, _body(iad, united, stop=stop.pk))

    dispatch_services.send_offer(res, VendorFactory(email="ops@capital.example"), payout=100)
    assert len(mailoutbox) == 1
    text = mailoutbox[0].body
    assert "flight UA 123 arr" in text
