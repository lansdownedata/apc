from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.reservations.timezones import resolve

pytestmark = pytest.mark.django_db


def _stop(*, timezone="", lat=None, lng=None):
    airport = SimpleNamespace(timezone=timezone) if timezone is not None else None
    return SimpleNamespace(airport=airport, latitude=lat, longitude=lng)


def test_resolve_prefers_airport_timezone_over_coordinates():
    stop = _stop(timezone="America/Los_Angeles", lat=38.9072, lng=-77.0369)
    assert resolve(stop) == "America/Los_Angeles"


def test_los_angeles_coordinates_resolve_to_pacific():
    stop = _stop(timezone="", lat=Decimal("34.052200"), lng=Decimal("-118.243700"))
    assert resolve(stop) == "America/Los_Angeles"


def test_virginia_coordinates_resolve_to_eastern():
    stop = _stop(timezone="", lat=Decimal("38.907200"), lng=Decimal("-77.036900"))
    assert resolve(stop) == "America/New_York"


def test_no_coordinates_and_no_airport_returns_empty():
    assert resolve(_stop(timezone="", lat=None, lng=None)) == ""


def test_ocean_none_from_finder_returns_empty_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        "apps.reservations.timezones._finder.timezone_at",
        lambda **kwargs: None,
    )
    assert resolve(_stop(timezone="", lat=0, lng=0)) == ""


def test_blank_airport_timezone_falls_through_to_coordinates():
    stop = _stop(timezone="", lat=Decimal("34.052200"), lng=Decimal("-118.243700"))
    stop.airport = SimpleNamespace(timezone="")
    assert resolve(stop) == "America/Los_Angeles"
