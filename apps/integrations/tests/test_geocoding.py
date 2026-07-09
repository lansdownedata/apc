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
