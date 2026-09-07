"""Drive time between two points — LocationIQ Directions.

Used to derive a trip's drop-off: a transfer ends at pickup + however long the drive
actually takes, or the vehicle's billed minimum, whichever is longer. Never raises and
never blocks a save: a missing key, an API error or a point with no coordinates all
return None, and the caller falls back to the minimum.
"""

import pytest
import requests
from django.core.cache import cache

from apps.integrations import geocoding

ROUTE = {"code": "Ok", "routes": [{"duration": 2700.0, "distance": 34000.0}]}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def key(settings):
    settings.LOCATIONIQ_API_KEY = "test-key"


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_it_returns_the_route_duration_in_seconds(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(ROUTE))
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1) == 2700


def test_a_blank_api_key_returns_none_rather_than_raising(settings, monkeypatch):
    settings.LOCATIONIQ_API_KEY = ""

    def _boom(*a, **k):
        raise AssertionError("must not call the API without a key")

    monkeypatch.setattr(requests, "get", _boom)
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1) is None


def test_a_missing_coordinate_returns_none(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(ROUTE))
    assert geocoding.drive_seconds(None, -77.4, 38.6, -77.1) is None
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, None) is None


def test_an_api_error_returns_none(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(None, status=429, text="slow down"))
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1) is None


def test_a_network_failure_returns_none(key, monkeypatch):
    def _raise(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", _raise)
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1) is None


def test_a_routeless_response_returns_none(key, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"code": "NoRoute", "routes": []}))
    assert geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1) is None


def test_the_same_pair_is_only_fetched_once(key, monkeypatch):
    calls = []

    def _count(*a, **k):
        calls.append(1)
        return _Resp(ROUTE)

    monkeypatch.setattr(requests, "get", _count)
    geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1)
    geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1)
    assert len(calls) == 1


def test_the_route_is_directional(key, monkeypatch):
    """A→B and B→A are different journeys and must not share a cache entry."""
    calls = []

    def _count(*a, **k):
        calls.append(1)
        return _Resp(ROUTE)

    monkeypatch.setattr(requests, "get", _count)
    geocoding.drive_seconds(38.9, -77.4, 38.6, -77.1)
    geocoding.drive_seconds(38.6, -77.1, 38.9, -77.4)
    assert len(calls) == 2
