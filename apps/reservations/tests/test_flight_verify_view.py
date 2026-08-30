"""The one verify endpoint both the editor and the drawer post to (spec §6.3)."""

import json
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.integrations.aviationstack import AviationstackError
from apps.reservations import views
from apps.reservations.factories import FlightFactory
from apps.reservations.flights import FlightLookupError

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


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


def _body(iad, united, **over):
    base = {
        "airport": iad.pk,
        "airline": united.pk,
        "flight": "123",
        "date": "2026-10-15",
        "direction": "arrival",
        "time": "16:45",
    }
    base.update(over)
    return base


def test_requires_login(client, iad, united):
    resp = _post(client, _body(iad, united))
    assert resp.status_code == 302 and "/portal/login/" in resp["Location"]


def test_get_is_not_allowed(staff):
    assert staff.get(reverse("flight_verify")).status_code == 405


def test_url_lives_under_portal_reservations():
    assert reverse("flight_verify") == "/portal/reservations/flights/verify/"


@pytest.mark.parametrize(
    "over, code",
    [
        ({"airport": 999999}, "airport"),
        ({"airline": ""}, "airline"),
        ({"flight": "UA12"}, "flight"),
        ({"direction": "sideways"}, "direction"),
        ({"direction": ""}, "direction"),
        ({"date": ""}, "date"),
        ({"date": "Oct 15"}, "date"),
    ],
)
def test_bad_input_is_400_with_a_code(staff, iad, united, over, code):
    resp = _post(staff, _body(iad, united, **over))
    assert resp.status_code == 400
    assert resp.json()["code"] == code and resp.json()["error"]


def test_invalid_json_is_400(staff):
    resp = staff.post(reverse("flight_verify"), data="{nope", content_type="application/json")
    assert resp.status_code == 400 and resp.json()["code"] == "bad_request"


def test_success_returns_the_pill_and_passes_the_time_through(staff, iad, united):
    row = FlightFactory(
        airline=united,
        airport=iad,
        flight_number="123",
        flight_date=date(2026, 10, 15),
        direction="arrival",
        scheduled_at=datetime(2026, 10, 15, 21, 35, tzinfo=UTC),
        checked_at=NOW,
    )
    with patch.object(views.flights, "lookup", return_value=row) as lookup:
        resp = _post(staff, _body(iad, united))
    assert resp.status_code == 200
    kwargs = lookup.call_args.kwargs
    assert kwargs["airline"] == united and kwargs["airport"] == iad
    assert kwargs["flight_number"] == "123" and kwargs["direction"] == "arrival"
    assert kwargs["flight_date"] == date(2026, 10, 15)
    assert kwargs["preferred_time"].strftime("%H:%M") == "16:45"
    body = resp.json()
    assert body["state"] == "verified" and body["label"] == "UA 123 · 5:35 PM EDT"
    assert body["refresh_allowed_at"] and body["chip"] == "chip-ok"


def test_blank_or_garbled_time_is_simply_no_preference(staff, iad, united):
    row = FlightFactory(airline=united, airport=iad)
    with patch.object(views.flights, "lookup", return_value=row) as lookup:
        _post(staff, _body(iad, united, time="noon"))
    assert lookup.call_args.kwargs["preferred_time"] is None


def test_a_retired_airline_can_still_be_looked_up(staff, iad, united):
    """The flight was booked on that carrier — retiring it stops new bookings, not lookups
    (spec §9)."""
    united.is_active = False
    united.save(update_fields=["is_active"])
    row = FlightFactory(airline=united, airport=iad)
    with patch.object(views.flights, "lookup", return_value=row):
        assert _post(staff, _body(iad, united)).status_code == 200


def test_lookup_refusal_is_400_with_its_code(staff, iad, united):
    with patch.object(
        views.flights,
        "lookup",
        side_effect=FlightLookupError("past_date", "The trip date has passed."),
    ):
        resp = _post(staff, _body(iad, united))
    assert resp.status_code == 400
    assert resp.json() == {"error": "The trip date has passed.", "code": "past_date"}


def test_an_airport_with_no_scheduled_service_is_400_without_calling_the_provider(
    staff, iad, united
):
    """Real (unmocked) flights.lookup — Andrews-style airports must be refused before any
    aviationstack call, not just when the caller happens to mock lookup() away."""
    iad.has_scheduled_service = False
    iad.save()
    with patch("apps.integrations.aviationstack.future_schedule") as future:
        resp = _post(staff, _body(iad, united))
    assert resp.status_code == 400
    assert resp.json()["code"] == "no_scheduled_service"
    future.assert_not_called()


@pytest.mark.parametrize(
    "code, message",
    [
        ("not_configured", "Flight verification isn't configured — add AVIATIONSTACK_API_KEY."),
        ("invalid_key", "Flight service rejected our API key."),
        ("plan", "Your aviationstack plan doesn't include this lookup."),
        ("rate_limited", "Flight service is busy — please try again in a moment."),
        ("quota", "Monthly flight lookups are used up."),
        ("transport", "Couldn't reach the flight service — try again."),
        ("server", "Couldn't reach the flight service — try again."),
    ],
)
def test_provider_errors_are_503_with_spec_copy(staff, iad, united, code, message):
    with patch.object(views.flights, "lookup", side_effect=AviationstackError(code, "x", 429)):
        resp = _post(staff, _body(iad, united))
    assert resp.status_code == 503
    assert resp.json() == {"error": message, "code": code}
