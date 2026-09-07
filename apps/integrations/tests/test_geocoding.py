"""LocationIQ forward geocoding + per-Stop row caching (requests mocked)."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.integrations import geocoding
from apps.leads.factories import LeadFactory
from apps.reservations.factories import ReservationFactory, StopFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def liq_key(settings):
    settings.LOCATIONIQ_API_KEY = "liq-key"


def _response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else []
    resp.text = text
    return resp


def test_geocode_returns_decimals():
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(json_data=[{"lat": "40.7580", "lon": "-73.9855"}])
        lat, lng = geocoding.geocode("Times Square, New York")
    assert (lat, lng) == (Decimal("40.7580"), Decimal("-73.9855"))
    assert req.get.call_args.kwargs["params"]["key"] == "liq-key"


def test_no_results_raises():
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(json_data=[])
        with pytest.raises(geocoding.GeocodeError):
            geocoding.geocode("xyzzy nowhere")


def test_missing_key_raises(settings):
    settings.LOCATIONIQ_API_KEY = ""
    with pytest.raises(geocoding.GeocodeError):
        geocoding.geocode("anywhere")


def test_empty_address_raises():
    with pytest.raises(geocoding.GeocodeError):
        geocoding.geocode("  ")


def test_geocode_stop_caches_on_row():
    stop = StopFactory(reservation=ReservationFactory(lead=LeadFactory()), address="JFK Airport")
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(json_data=[{"lat": "40.6413", "lon": "-73.7781"}])
        geocoding.geocode_stop(stop)
        geocoding.geocode_stop(stop)  # second call served from the row
    assert req.get.call_count == 1
    stop.refresh_from_db()
    assert stop.latitude == Decimal("40.6413")


# --- autocomplete caching + rate limiting -------------------------------------
# Probed against the live key 2026-09-06: LocationIQ 429s from the second request
# inside a second. The typeahead's debounce lets a normal typist outrun that, and a
# 429 used to be indistinguishable from "no such place" — the dropdown just emptied.
def _poi(name="Days Inn"):
    return [
        {
            "place_id": "1",
            "lat": "39.1",
            "lon": "-77.5",
            "class": "tourism",
            "type": "hotel",
            "address": {"name": name},
            "display_name": name,
        }
    ]


def test_autocomplete_caches_a_successful_lookup():
    """Backspacing over a query must not re-buy the answer we just paid for."""
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(json_data=_poi())
        first = geocoding.autocomplete("days inn leesburg")
        second = geocoding.autocomplete("days inn leesburg")
    assert first == second
    assert req.get.call_count == 1


def test_the_cache_key_separates_different_queries():
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(json_data=_poi())
        geocoding.autocomplete("days inn leesburg")
        geocoding.autocomplete("hampton inn leesburg")
    assert req.get.call_count == 2


def test_a_rate_limited_lookup_is_never_cached():
    """Caching a 429's empty list would hide the real place for the whole TTL —
    strictly worse than paying for the retry."""
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(status=429, text='{"error":"Rate Limited"}')
        assert geocoding.autocomplete("red roof leesburg") == []
        req.get.return_value = _response(json_data=_poi("Red Roof Inn"))
        assert geocoding.autocomplete("red roof leesburg")
    assert req.get.call_count == 2


def test_a_rate_limited_lookup_is_logged_rather_than_passing_as_no_results(caplog):
    with patch.object(geocoding, "requests") as req:
        req.get.return_value = _response(status=429, text='{"error":"Rate Limited"}')
        geocoding.autocomplete("red roof leesburg")
    assert any("429" in r.getMessage() for r in caplog.records)
