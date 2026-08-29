"""aviationstack client — request plumbing + normalization of both endpoint shapes.

Never a real call: `requests` is mocked at the boundary. Fixture bodies are real probe
output captured on Moe's key 2026-08-29 (task-3R-brief.md), not the published docs — the
docs turned out to be wrong on several points (see aviationstack.py's module docstring).
`_timetable_entry()`'s defaults are the LXJ561 entry verbatim from
docs/aviationstack/probes/timetable-IAD-arrival-sample.json.
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
    """flightsFuture entry shape. `scheduledTime` is real ground truth (2026-08-29 probe):
    space-separated `"YYYY-MM-DD HH:MM:SS"`, matching FUTURE_KW's date — NOT the bare
    `"HH:MM"` the published docs describe (still accepted; see test_hhmm_parses_*)."""
    entry = {
        "weekday": "4",
        "departure": {
            "iataCode": "DEN",
            "icaoCode": "KDEN",
            "terminal": "B",
            "gate": "B31",
            "scheduledTime": "2026-10-15 12:20:00",
        },
        "arrival": {
            "iataCode": "IAD",
            "icaoCode": "KIAD",
            "terminal": "C",
            "gate": "C7",
            "scheduledTime": "2026-10-15 17:35:00",
        },
        "aircraft": {"modelCode": "B739", "modelText": "Boeing 737-900"},
        "airline": {"name": "United Airlines", "iataCode": "UA", "icaoCode": "UAL"},
        "flight": {"number": "123", "iataNumber": "UA123", "icaoNumber": "UAL123"},
        "codeshared": None,
    }
    entry.update(over)
    return entry


def _timetable_entry(**over):
    """`/v1/timetable` entry — the LXJ561 arrival verbatim from
    docs/aviationstack/probes/timetable-IAD-arrival-sample.json (real key, 2026-08-29).
    Naive ISO-T `scheduledTime`s are airport-local, not UTC (task-3R-brief.md §2)."""
    entry = {
        "airline": {"iataCode": "LXJ", "icaoCode": "LXJ", "name": "Flexjet"},
        "arrival": {
            "iataCode": "IAD",
            "icaoCode": "KIAD",
            "scheduledTime": "2026-08-29T12:06:00.000",
            "estimatedTime": None,
            "actualTime": None,
            "delay": None,
            "terminal": None,
            "gate": None,
        },
        "departure": {
            "iataCode": "TEB",
            "icaoCode": "KTEB",
            "scheduledTime": "2026-08-29T11:10:00.000",
            "estimatedTime": "2026-08-29T11:26:00.000",
            "actualTime": "2026-08-29T11:26:00.000",
            "delay": "16",
            "terminal": None,
            "gate": None,
        },
        "flight": {"iataNumber": "LXJ561", "icaoNumber": "LXJ561", "number": "561"},
        "codeshared": None,
        "status": "scheduled",
        "type": "arrival",
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
    date=date(2026, 8, 29),
    airline_iata="LXJ",
    flight_number="561",
    airport_tz="America/New_York",  # IAD — a non-UTC airport, so an offset bug can't hide
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


def test_live_flight_sends_iataCode_type_and_flight_iata_to_timetable():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_timetable_entry()]})
        av.live_flight(**LIVE_KW)
    url = req.get.call_args.args[0]
    params = req.get.call_args.kwargs["params"]
    assert url == "https://api.aviationstack.test/v1/timetable"
    assert params == {
        "iataCode": "IAD",
        "type": "arrival",
        "flight_iata": "LXJ561",
        "access_key": "test-key",
    }


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


# --- _hhmm ---


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-09-28 20:00:00", time(20, 0)),  # real flightsFuture shape (2026-08-29 probe)
        ("20:00", time(20, 0)),  # documented bare HH:MM — still accepted
        ("2026-09-28T20:00:00", time(20, 0)),  # older-docs ISO-T form — still accepted
        ("", None),
        (None, None),
        ("not a time", None),
    ],
)
def test_hhmm_parses_every_observed_shape(raw, expected):
    """Verified against the merged code (task-3R-brief.md §1):
    `_hhmm("20:00")` -> 20:00:00 but `_hhmm("2026-09-28 20:00:00")` -> None. The real API
    sends the space-separated form, so that was silently dropping every far-future time."""
    assert av._hhmm(raw) == expected


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


def test_future_real_entry_parses_space_separated_time_and_title_cases_display():
    """The real flightsFuture entry verbatim from task-3R-brief.md: lowercase everywhere,
    space-separated `scheduledTime`. Pins both the critical parsing fix (§1) and the
    display-string title-casing (§4) — a dispatcher should see "Gate C3", not "Gate c3"."""
    entry = {
        "weekday": "1",
        "departure": {
            "iataCode": "lhr",
            "icaoCode": "egll",
            "terminal": "2",
            "gate": "",
            "scheduledTime": "2026-09-28 16:40:00",
        },
        "arrival": {
            "iataCode": "iad",
            "icaoCode": "kiad",
            "terminal": "",
            "gate": "c3",
            "scheduledTime": "2026-09-28 20:00:00",
        },
        "aircraft": {"modelCode": "b772", "modelText": "boeing 777-222(er)"},
        "airline": {"name": "air canada", "iataCode": "ac", "icaoCode": "aca"},
        "flight": {"number": "5351", "iataNumber": "ac5351", "icaoNumber": "aca5351"},
        "codeshared": None,
    }
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [entry]})
        r = av.future_schedule(
            airport_iata="IAD",
            direction="arrival",
            date=date(2026, 9, 28),
            airline_iata="AC",
            flight_number="5351",
            airport_tz="America/New_York",
        )
    assert r.found
    assert r.scheduled_at == datetime(2026, 9, 29, 0, 0, tzinfo=UTC)  # 20:00 EDT -> next-day UTC
    assert r.scheduled_at is not None  # the headline bug: this used to be None
    assert r.gate == "C3"  # title-cased, not the raw lowercase "c3"
    assert r.terminal == ""
    assert r.other_airport_iata == "LHR"  # code matching is untouched — still upper-cased


def test_future_codeshare_operated_by_name_is_title_cased():
    entry = _future_entry(
        codeshared={
            "airline": {"name": "air canada", "iataCode": "AC", "icaoCode": "ACA"},
            "flight": {"number": "5351", "iataNumber": "AC5351", "icaoNumber": "ACA5351"},
        }
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [[entry]]})
        r = av.future_schedule(**FUTURE_KW)
    assert r.operated_by_name == "Air Canada"


# --- /v1/timetable normalization (the live path — /v1/flights 403s on this plan) ---


def test_live_arrival_reads_status_times_delay_and_keeps_only_our_airport():
    elsewhere = _timetable_entry(
        arrival={"iataCode": "EWR", "scheduledTime": "2026-08-29T12:06:00.000"}
    )
    ours = _timetable_entry(
        arrival={
            "iataCode": "IAD",
            "icaoCode": "KIAD",
            "scheduledTime": "2026-08-29T12:48:00.000",
            "estimatedTime": "2026-08-29T12:59:00.000",
            "actualTime": "2026-08-29T12:59:00.000",
            "delay": "11",
            "terminal": None,
            "gate": "B73",
        },
        status="landed",
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [elsewhere, ours]})
        r = av.live_flight(**LIVE_KW)
    assert r.found and r.status == "landed"
    assert r.scheduled_at == datetime(2026, 8, 29, 16, 48, tzinfo=UTC)  # 12:48 EDT -> UTC
    assert r.estimated_at == datetime(2026, 8, 29, 16, 59, tzinfo=UTC)
    assert r.actual_at == datetime(2026, 8, 29, 16, 59, tzinfo=UTC)
    assert r.delay_minutes == 11
    assert (r.terminal, r.gate) == ("", "B73")
    assert (r.other_airport_iata, r.other_airport_name) == ("TEB", "")  # no airport-name field


def test_live_departure_reads_the_departure_block():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_timetable_entry()]})
        r = av.live_flight(**{**LIVE_KW, "airport_iata": "TEB", "direction": "departure"})
    assert r.actual_at == datetime(2026, 8, 29, 15, 26, tzinfo=UTC)  # 11:26 EDT -> UTC
    assert r.delay_minutes == 16 and r.other_airport_iata == "IAD"


def test_live_null_terminal_gate_and_delay_are_tolerated():
    """The arrival block on the real LXJ561 sample has terminal/gate/delay all null."""
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_timetable_entry()]})
        r = av.live_flight(**LIVE_KW)
    assert (r.terminal, r.gate, r.delay_minutes) == ("", "", None)


def test_live_unknown_status_falls_back_to_scheduled():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [_timetable_entry(status="unknown")]})
        r = av.live_flight(**LIVE_KW)
    assert r.status == "scheduled"


def test_live_redirected_status_maps_to_diverted():
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(
            json_data={"data": [_timetable_entry(status="redirected")]}
        )
        r = av.live_flight(**LIVE_KW)
    assert r.status == "diverted"


def test_live_naive_timetable_timestamp_is_airport_local_not_utc():
    """The trap named in task-3R-brief.md §2: /v1/timetable's `scheduledTime` has no UTC
    offset, and it is airport-local — not UTC. Reusing `_iso_utc` (which treats a naive
    value as UTC, correct for the old /v1/flights shape) would silently be off by the
    airport's offset. IAD is UTC-04:00 in August, chosen so a 4-hour bug can't hide behind
    a UTC test airport."""
    entry = _timetable_entry(
        arrival={"iataCode": "IAD", "scheduledTime": "2026-08-29T12:06:00.000"}
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [entry]})
        r = av.live_flight(**LIVE_KW)
    wrong_if_treated_as_utc = datetime(2026, 8, 29, 12, 6, tzinfo=UTC)
    correct_local_to_utc = datetime(2026, 8, 29, 16, 6, tzinfo=UTC)  # 12:06 EDT -> UTC
    assert r.scheduled_at == correct_local_to_utc
    assert r.scheduled_at != wrong_if_treated_as_utc


def test_live_codeshare_operated_by():
    """`codeshared` on /v1/timetable is the nested `{"airline": {...}, "flight": {...}}`
    form (like flightsFuture) — NOT the flat `airline_iata`/`airline_name` keys the old
    /v1/flights shape used."""
    entry = _timetable_entry(
        codeshared={
            "airline": {"iataCode": "AA", "icaoCode": "AAL", "name": "American Airlines"},
            "flight": {"iataNumber": "AA9876", "icaoNumber": "AAL9876", "number": "9876"},
        }
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [entry]})
        r = av.live_flight(**LIVE_KW)
    assert (r.operated_by_iata, r.operated_by_name) == ("AA", "American Airlines")


def test_live_several_matches_prefer_closest_to_preferred_time_in_local_time():
    """`preferred_time` is always airport-local wall-clock. Unlike the old /v1/flights shape
    (aware UTC, where the Critical bug from the earlier review lived), timetable's
    scheduledTime is already naive local — matching preferred_time directly is correct here
    by construction. This pins that `_pick` still picks the closer of two candidates."""
    early = _timetable_entry(
        arrival={"iataCode": "IAD", "scheduledTime": "2026-08-29T09:00:00.000"}
    )
    exact = _timetable_entry(
        arrival={"iataCode": "IAD", "scheduledTime": "2026-08-29T09:10:00.000"}
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [early, exact]})
        r = av.live_flight(**LIVE_KW, preferred_time=time(9, 10))
    assert r.scheduled_at == datetime(2026, 8, 29, 13, 10, tzinfo=UTC)  # 09:10 EDT, the exact match


def test_live_no_entry_at_our_airport_is_not_found():
    elsewhere = _timetable_entry(
        arrival={"iataCode": "EWR", "scheduledTime": "2026-08-29T12:06:00.000"}
    )
    with patch.object(av, "requests") as req:
        req.get.return_value = _response(json_data={"data": [elsewhere]})
        r = av.live_flight(**LIVE_KW)
    assert r == av.NOT_FOUND


def test_is_configured(settings):
    assert av.is_configured()
    settings.AVIATIONSTACK_API_KEY = ""
    assert not av.is_configured()
