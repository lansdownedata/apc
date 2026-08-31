from io import StringIO

import pytest
from django.core.management import call_command

from apps.addresses.factories import AirportFactory
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Stop

pytestmark = pytest.mark.django_db


def _pickup(res, **kwargs):
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(reservation=res, sequence=0, **kwargs)
    Stop.objects.create(reservation=res, sequence=1, address="Drop")
    return res


def test_backfill_classifies_airport_coords_and_fallback():
    airport = AirportFactory(timezone="America/Los_Angeles")
    by_airport = _pickup(
        TransferReservationFactory(pickup_timezone=""),
        address="LAX",
        airport=airport,
        latitude="33.941600",
        longitude="-118.408500",
    )
    by_coords = _pickup(
        TransferReservationFactory(pickup_timezone=""),
        address="Santa Monica",
        latitude="34.019400",
        longitude="-118.491200",
    )
    fallback = _pickup(
        TransferReservationFactory(pickup_timezone=""),
        address="typed by hand",
    )
    out = StringIO()
    call_command("backfill_trip_timezones", stdout=out)
    report = out.getvalue()
    assert "airport=1" in report
    assert "coords=1" in report
    assert "fallback=1" in report
    by_airport.refresh_from_db()
    by_coords.refresh_from_db()
    fallback.refresh_from_db()
    assert by_airport.pickup_timezone == "America/Los_Angeles"
    assert by_coords.pickup_timezone == "America/Los_Angeles"
    assert fallback.pickup_timezone == ""


def test_backfill_is_idempotent():
    _pickup(
        TransferReservationFactory(pickup_timezone="America/New_York"),
        address="already set",
        latitude="38.907200",
        longitude="-77.036900",
    )
    out = StringIO()
    call_command("backfill_trip_timezones", stdout=out)
    assert "airport=0" in out.getvalue()
    assert "coords=0" in out.getvalue()
    assert "fallback=0" in out.getvalue()
