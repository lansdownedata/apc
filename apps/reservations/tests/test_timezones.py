from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings

from apps.addresses.factories import AirportFactory
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Stop
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


def test_unusable_coordinates_return_empty_not_a_crash():
    stop = _stop(timezone="", lat=Decimal("999"), lng=Decimal("-118.243700"))
    assert resolve(stop) == ""


def test_pickup_at_is_aware_in_the_trip_zone():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="America/Los_Angeles",
    )
    instant = res.pickup_at
    assert instant == datetime(2026, 9, 14, 7, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert instant.utcoffset().total_seconds() == -7 * 3600  # PDT


def test_pickup_at_is_none_without_a_date():
    res = TransferReservationFactory(pickup_date=None, pickup_time=time(7, 30))
    assert res.pickup_at is None


def test_pickup_at_falls_back_to_project_zone_when_unresolved():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="",
    )
    assert res.pickup_at.tzinfo == ZoneInfo(settings.TIME_ZONE)


def test_pickup_tz_abbrev_is_blank_when_trip_is_in_project_zone():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone=settings.TIME_ZONE,
    )
    assert res.pickup_tz_abbrev == ""


def test_pickup_tz_abbrev_is_pdt_for_a_pacific_pickup_in_september():
    res = TransferReservationFactory(
        pickup_date=date(2026, 9, 14),
        pickup_time=time(7, 30),
        pickup_timezone="America/Los_Angeles",
    )
    assert res.pickup_tz_abbrev == "PDT"


def test_refresh_is_noop_when_zone_is_unchanged():
    airport = AirportFactory(timezone="America/New_York")
    res = TransferReservationFactory(pickup_timezone="America/New_York")
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="IAD",
        airport=airport,
        latitude="38.953100",
        longitude="-77.456500",
    )
    Stop.objects.create(reservation=res, sequence=1, address="Drop")
    before = res.updated_at
    changed = res.refresh_pickup_timezone()
    res.refresh_from_db()
    assert changed is False
    assert res.pickup_timezone == "America/New_York"
    assert res.updated_at == before


def test_refresh_re_resolves_when_the_pickup_address_changes():
    res = TransferReservationFactory(pickup_timezone="America/New_York")
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="LAX",
        latitude="33.941600",
        longitude="-118.408500",
    )
    Stop.objects.create(reservation=res, sequence=1, address="Drop")
    assert res.refresh_pickup_timezone() is True
    res.refresh_from_db()
    assert res.pickup_timezone == "America/Los_Angeles"
