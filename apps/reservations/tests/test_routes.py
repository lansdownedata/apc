"""Full-route geometry for the driver page: fetched once, cached on the Reservation."""

from decimal import Decimal

import pytest
import requests

from apps.integrations import geocoding
from apps.reservations import routes
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


OK = {"code": "Ok", "routes": [{"geometry": "_p~iF~ps|U_ulLnnqC"}]}


@pytest.fixture
def key(settings):
    settings.LOCATIONIQ_API_KEY = "test-key"


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []

    def fake_get(url, **kwargs):
        seen.append((url, kwargs))
        return _Resp(OK)

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


def _trip():
    trip = TransferReservationFactory()
    for stop, (lat, lng) in zip(trip.ordered_stops, [(38.9, -77.4), (38.6, -77.1)], strict=True):
        stop.latitude, stop.longitude = Decimal(str(lat)), Decimal(str(lng))
        stop.save(update_fields=["latitude", "longitude", "updated_at"])
    return trip


def test_route_polyline_sends_all_stops_in_order_lon_first(key, calls):
    out = geocoding.route_polyline([(38.9, -77.4), (38.7, -77.2), (38.6, -77.1)])
    assert out == OK["routes"][0]["geometry"]
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"{geocoding.DIRECTIONS_URL}/-77.4,38.9;-77.2,38.7;-77.1,38.6"
    assert kwargs["params"] == {
        "key": "test-key",
        "overview": "full",
        "geometries": "polyline",
    }


def test_route_polyline_none_without_key(settings, monkeypatch):
    settings.LOCATIONIQ_API_KEY = ""

    def boom(*a, **k):
        raise AssertionError("no call without a key")

    monkeypatch.setattr(requests, "get", boom)
    assert geocoding.route_polyline([(38.9, -77.4), (38.6, -77.1)]) is None


def test_route_polyline_none_with_fewer_than_two_points(key, calls):
    assert geocoding.route_polyline([(38.9, -77.4)]) is None
    assert calls == []


def test_route_polyline_none_on_http_error(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({}, status=429, text="slow down"))
    assert geocoding.route_polyline([(38.9, -77.4), (38.6, -77.1)]) is None


def test_route_polyline_none_on_malformed_payload(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"routes": []}))
    assert geocoding.route_polyline([(38.9, -77.4), (38.6, -77.1)]) is None


def test_route_is_computed_once_then_cached(key, calls):
    trip = _trip()
    assert routes.route_for(trip) == OK["routes"][0]["geometry"]
    assert routes.route_for(trip) == OK["routes"][0]["geometry"]
    assert len(calls) == 1
    trip.refresh_from_db()
    assert trip.route_geometry == OK["routes"][0]["geometry"]
    assert trip.route_signature == routes.stop_signature(trip.ordered_stops)


def test_route_recomputed_when_a_stop_moves(key, calls):
    trip = _trip()
    routes.route_for(trip)
    last = trip.ordered_stops.last()
    last.latitude = Decimal("38.5")
    last.save(update_fields=["latitude", "updated_at"])
    routes.route_for(trip)
    assert len(calls) == 2


def test_failure_caches_nothing(key, monkeypatch):
    trip = _trip()
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({}, status=500))
    assert routes.route_for(trip) == ""
    trip.refresh_from_db()
    assert trip.route_geometry == "" and trip.route_signature == ""

    from django.core.cache import cache

    cache.clear()  # the failure above is remembered for ten minutes
    seen = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: seen.append(1) or _Resp(OK))
    assert routes.route_for(trip) == OK["routes"][0]["geometry"]
    assert len(seen) == 1


def test_stop_without_coordinates_returns_blank_without_calling(key, calls):
    trip = _trip()
    last = trip.ordered_stops.last()
    last.latitude = None
    last.save(update_fields=["latitude", "updated_at"])
    assert routes.route_for(trip) == ""
    assert calls == []


def test_route_polyline_uses_its_own_short_timeout(key, calls):
    geocoding.route_polyline([(38.9, -77.4), (38.6, -77.1)])
    assert calls[0][1]["timeout"] == geocoding.ROUTE_TIMEOUT == 4


def test_route_polyline_logs_a_request_exception_without_the_key(key, monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "get", boom)
    with caplog.at_level("WARNING"):
        assert geocoding.route_polyline([(38.9, -77.4), (38.6, -77.1)]) is None
    assert "LocationIQ route" in caplog.text
    assert "test-key" not in caplog.text


def test_failed_lookup_is_not_retried_within_ten_minutes(key, monkeypatch):
    trip = _trip()
    seen = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: seen.append(1) or _Resp({}, status=500))
    assert routes.route_for(trip) == ""
    assert routes.route_for(trip) == ""
    assert len(seen) == 1


def test_changed_stops_retry_after_a_failure(key, monkeypatch):
    trip = _trip()
    seen = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: seen.append(1) or _Resp({}, status=500))
    routes.route_for(trip)
    last = trip.ordered_stops.last()
    last.latitude = Decimal("38.5")
    last.save(update_fields=["latitude", "updated_at"])
    routes.route_for(trip)
    assert len(seen) == 2
