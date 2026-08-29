"""aviationstack client — request plumbing + normalization of both endpoint shapes.

Never a real call: `requests` is mocked at the boundary. Fixture bodies are the documented
examples adapted to IAD; replace them with trimmed probe output once Moe has run
`manage.py aviationstack_probe` (docs/aviationstack/probes/).
"""

from datetime import UTC, date, datetime, time
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.integrations import aviationstack as av


@pytest.fixture(autouse=True)
def key(settings):
    settings.AVIATIONSTACK_API_KEY = "test-key"
    settings.AVIATIONSTACK_BASE_URL = "https://api.aviationstack.test"


def _response(status=200, json_data=None, text="", raise_json=False):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.content = b"x" if (json_data is not None or text) else b""
    if raise_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _future_entry(**over):
    entry = {
        "weekday": "4",
        "departure": {
            "iataCode": "DEN",
            "icaoCode": "KDEN",
            "terminal": "B",
            "gate": "B31",
            "scheduledTime": "12:20",
        },
        "arrival": {
            "iataCode": "IAD",
            "icaoCode": "KIAD",
            "terminal": "C",
            "gate": "C7",
            "scheduledTime": "17:35",
        },
        "aircraft": {"modelCode": "B739", "modelText": "Boeing 737-900"},
        "airline": {"name": "United Airlines", "iataCode": "UA", "icaoCode": "UAL"},
        "flight": {"number": "123", "iataNumber": "UA123", "icaoNumber": "UAL123"},
        "codeshared": None,
    }
    entry.update(over)
    return entry


def _live_entry(**over):
    entry = {
        "flight_date": "2026-09-02",
        "flight_status": "active",
        "departure": {
            "airport": "Denver International",
            "timezone": "America/Denver",
            "iata": "DEN",
            "icao": "KDEN",
            "terminal": "B",
            "gate": "B31",
            "delay": 12,
            "scheduled": "2026-09-02T12:20:00+00:00",
            "estimated": "2026-09-02T12:32:00+00:00",
            "actual": "2026-09-02T12:35:00+00:00",
        },
        "arrival": {
            "airport": "Washington Dulles International",
            "timezone": "America/New_York",
            "iata": "IAD",
            "icao": "KIAD",
            "terminal": "C",
            "gate": "C7",
            "delay": 40,
            "scheduled": "2026-09-02T21:35:00+00:00",
            "estimated": "2026-09-02T22:15:00+00:00",
            "actual": None,
        },
        "airline": {"name": "United Airlines", "iata": "UA", "icao": "UAL"},
        "flight": {"number": "123", "iata": "UA123", "icao": "UAL123", "codeshared": None},
        "aircraft": None,
        "live": None,
    }
    entry.update(over)
    return entry


FUTURE_KW = dict(
    airport_iata="IAD",
    direction="arrival",
    date=date(2026, 10, 15),
    airline_iata="UA",
    flight_number="123",
    airport_tz="America/New_York",
)
LIVE_KW = dict(
    airport_iata="IAD",
    direction="arrival",
    date=date(2026, 9, 2),
    airline_iata="UA",
    flight_number="123",
)


# --- request plumbing ---


def test_blank_key_raises_without_calling(settings):
    settings.AVIATIONSTACK_API_KEY = ""
    with patch.object(av, "requests") as req:
        with pytest.raises(av.AviationstackError) as exc:
            av.future_schedule(**FUTURE_KW)
    assert exc.value.code == "not_configured"
    req.get.assert_not_called()


def test_future_schedule_sends_the_documented_params():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[_future_entry()]]})
        av.future_schedule(**FUTURE_KW)
    url = req.get.call_args.args[0]
    params = req.get.call_args.kwargs["params"]
    assert url == "https://api.aviationstack.test/v1/flightsFuture"
    assert params == {
        "iataCode": "IAD",
        "type": "arrival",
        "date": "2026-10-15",
        "airline_iata": "UA",
        "flight_number": "123",
        "access_key": "test-key",
    }
    assert req.get.call_args.kwargs["timeout"] == av.TIMEOUT


def test_live_flight_sends_flight_iata_and_date():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_live_entry()]})
        av.live_flight(**LIVE_KW)
    params = req.get.call_args.kwargs["params"]
    assert req.get.call_args.args[0].endswith("/v1/flights")
    assert params["flight_iata"] == "UA123" and params["flight_date"] == "2026-09-02"


@pytest.mark.parametrize(
    "status, api_code, expected",
    [
        (401, "invalid_access_key", "invalid_key"),
        (403, "function_access_restricted", "plan"),
        (404, "invalid_api_function", "not_found_endpoint"),
        (429, "rate_limit_reached", "rate_limited"),
        (429, "usage_limit_reached", "quota"),
        (500, "internal_error", "server"),
    ],
)
def test_error_bodies_map_to_codes(status, api_code, expected):
    body = {"error": {"code": api_code, "message": "nope"}}
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(status=status, json_data=body)
        with pytest.raises(av.AviationstackError) as exc:
            av.future_schedule(**FUTURE_KW)
    assert exc.value.code == expected
    assert exc.value.status == status
    assert "nope" in exc.value.message


def test_transport_failure_maps_to_transport():
    with patch.object(av, "requests") as req:
        req.RequestException = requests.RequestException
        req.get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(av.AviationstackError) as exc:
            av.live_flight(**LIVE_KW)
    assert exc.value.code == "transport"


def test_non_json_body_maps_to_bad_response():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(status=200, text="<html>", raise_json=True)
        with pytest.raises(av.AviationstackError) as exc:
            av.live_flight(**LIVE_KW)
    assert exc.value.code == "bad_response"


def test_empty_data_is_not_found_not_an_error():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": []})
        result = av.future_schedule(**FUTURE_KW)
    assert result == av.NOT_FOUND


# --- flightsFuture normalization ---


def test_future_arrival_reads_the_arrival_block_in_the_airport_zone():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[_future_entry()]]})
        r = av.future_schedule(**FUTURE_KW)
    assert r.found and r.status == "scheduled"
    assert r.scheduled_at == datetime(2026, 10, 15, 21, 35, tzinfo=UTC)  # 17:35 EDT
    assert (r.terminal, r.gate) == ("C", "C7")
    assert r.other_airport_iata == "DEN"
    assert r.estimated_at is None and r.actual_at is None and r.delay_minutes is None
    assert r.raw["flight"]["iataNumber"] == "UA123"


def test_future_departure_reads_the_departure_block():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[_future_entry()]]})
        r = av.future_schedule(
            **{
                **FUTURE_KW,
                "airport_iata": "DEN",
                "direction": "departure",
                "airport_tz": "America/Denver",
            }
        )
    assert r.scheduled_at == datetime(2026, 10, 15, 18, 20, tzinfo=UTC)  # 12:20 MDT
    assert (r.terminal, r.gate, r.other_airport_iata) == ("B", "B31", "IAD")


def test_future_accepts_a_flat_data_list():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_future_entry()]})
        r = av.future_schedule(**FUTURE_KW)
    assert r.found and r.terminal == "C"


def test_future_dst_end_ambiguous_time_does_not_raise():
    entry = _future_entry(arrival={"iataCode": "IAD", "scheduledTime": "01:30"})
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[entry]]})
        r = av.future_schedule(**{**FUTURE_KW, "date": date(2026, 11, 1)})
    assert r.scheduled_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # fold=0 → EDT


def test_future_codeshare_names_the_operating_carrier():
    entry = _future_entry(
        airline={"name": "Lufthansa", "iataCode": "LH", "icaoCode": "DLH"},
        flight={"number": "7601", "iataNumber": "LH7601", "icaoNumber": "DLH7601"},
        codeshared={
            "airline": {"name": "United Airlines", "iataCode": "UA", "icaoCode": "UAL"},
            "flight": {"number": "123", "iataNumber": "UA123", "icaoNumber": "UAL123"},
        },
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[entry]]})
        # Asked for LH 7601 → operated by United.
        r = av.future_schedule(**{**FUTURE_KW, "airline_iata": "LH", "flight_number": "7601"})
    assert (r.operated_by_iata, r.operated_by_name) == ("UA", "United Airlines")


def test_future_codeshare_of_the_queried_airline_is_not_reported_as_operated_by():
    """Filtering by UA 123 can return the LH 7601 entry whose codeshared block IS UA 123 —
    that flight is operated by United, so nothing to append."""
    entry = _future_entry(
        airline={"name": "Lufthansa", "iataCode": "LH", "icaoCode": "DLH"},
        flight={"number": "7601", "iataNumber": "LH7601", "icaoNumber": "DLH7601"},
        codeshared={
            "airline": {"name": "United Airlines", "iataCode": "UA", "icaoCode": "UAL"},
            "flight": {"number": "123", "iataNumber": "UA123", "icaoNumber": "UAL123"},
        },
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[entry]]})
        r = av.future_schedule(**FUTURE_KW)
    assert r.operated_by_iata == ""


def test_several_matches_prefer_direct_then_closest_to_preferred_time():
    morning = _future_entry(arrival={"iataCode": "IAD", "scheduledTime": "07:10"})
    evening = _future_entry(arrival={"iataCode": "IAD", "scheduledTime": "17:35"})
    codeshare = _future_entry(
        arrival={"iataCode": "IAD", "scheduledTime": "16:40"},
        codeshared={"airline": {"iataCode": "LH"}, "flight": {}},
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[codeshare, morning, evening]]})
        r = av.future_schedule(**FUTURE_KW, preferred_time=time(16, 45))
    assert r.scheduled_at.astimezone(UTC).hour == 21  # 17:35 EDT, not the codeshare at 16:40


# --- /v1/flights normalization ---


def test_live_arrival_reads_status_times_delay_and_keeps_only_our_airport():
    elsewhere = _live_entry(arrival={"iata": "EWR", "scheduled": "2026-09-02T20:00:00+00:00"})
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [elsewhere, _live_entry()]})
        r = av.live_flight(**LIVE_KW)
    assert r.found and r.status == "active"
    assert r.scheduled_at == datetime(2026, 9, 2, 21, 35, tzinfo=UTC)
    assert r.estimated_at == datetime(2026, 9, 2, 22, 15, tzinfo=UTC)
    assert r.actual_at is None
    assert r.delay_minutes == 40
    assert (r.terminal, r.gate) == ("C", "C7")
    assert (r.other_airport_iata, r.other_airport_name) == ("DEN", "Denver International")


def test_live_departure_reads_the_departure_block():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_live_entry()]})
        r = av.live_flight(**{**LIVE_KW, "airport_iata": "DEN", "direction": "departure"})
    assert r.actual_at == datetime(2026, 9, 2, 12, 35, tzinfo=UTC)
    assert r.delay_minutes == 12 and r.other_airport_iata == "IAD"


def test_live_unknown_status_falls_back_to_scheduled():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_live_entry(flight_status="weird")]})
        r = av.live_flight(**LIVE_KW)
    assert r.status == "scheduled"


def test_live_naive_timestamp_is_treated_as_utc():
    entry = _live_entry(arrival={"iata": "IAD", "scheduled": "2026-09-02T21:35:00.000"})
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [entry]})
        r = av.live_flight(**LIVE_KW)
    assert r.scheduled_at == datetime(2026, 9, 2, 21, 35, tzinfo=UTC)


def test_live_codeshare_operated_by():
    entry = _live_entry(
        flight={
            "number": "7601",
            "iata": "LH7601",
            "icao": "DLH7601",
            "codeshared": {
                "airline_name": "United Airlines",
                "airline_iata": "UA",
                "flight_iata": "UA123",
            },
        }
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [entry]})
        r = av.live_flight(**{**LIVE_KW, "airline_iata": "LH", "flight_number": "7601"})
    assert (r.operated_by_iata, r.operated_by_name) == ("UA", "United Airlines")


def test_live_no_entry_at_our_airport_is_not_found():
    elsewhere = _live_entry(arrival={"iata": "EWR", "scheduled": "2026-09-02T20:00:00+00:00"})
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [elsewhere]})
        r = av.live_flight(**LIVE_KW)
    assert r == av.NOT_FOUND


def test_is_configured(settings):
    assert av.is_configured()
    settings.AVIATIONSTACK_API_KEY = ""
    assert not av.is_configured()
