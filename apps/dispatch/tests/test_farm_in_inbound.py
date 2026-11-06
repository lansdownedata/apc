from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.dispatch import farm_in
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.reservations import timezones

pytestmark = pytest.mark.django_db


def test_timezone_at_beverly_hills():
    assert timezones.timezone_at(34.0664703, -118.3993243) == "America/Los_Angeles"
    assert timezones.timezone_at(None, -118.0) is None


def test_summarize_sample():
    sedan = VehicleTypeFactory(name="Luxury Sedan")
    s = farm_in.summarize(sample_payload())
    assert s.vehicle == sedan and s.gnet_vehicle_type == "SEDAN" and s.hourly is False
    assert s.timezone == "America/Los_Angeles" and s.timezone_assumed is False
    assert s.pickup_at == datetime(2024, 8, 3, 17, 27, 22, tzinfo=UTC)
    assert s.pickup_summary.startswith("123 S Beverly Dr") and s.dropoff_summary == "LAX"
    assert s.passenger_name == "Alex Smith" and s.passenger_count == 2
    assert s.alerts == []


def test_pickup_time_with_offset_is_not_double_shifted():
    p = sample_payload()
    p["locations"]["pickup"]["time"] = "2024-08-03T10:27:22-07:00"
    assert farm_in.summarize(p).pickup_at == datetime(2024, 8, 3, 17, 27, 22, tzinfo=UTC)


def test_summarize_unmapped_vehicle():
    p = sample_payload()
    p["preferredVehicleType"] = "GREETER"
    s = farm_in.summarize(p)
    assert s.vehicle is None and "Unmapped vehicle type: GREETER" in s.alerts


def test_summarize_missing_coordinates_assumes_default_zone(settings):
    settings.TIME_ZONE = "America/New_York"
    p = sample_payload()
    p["locations"]["pickup"].pop("lat")
    p["locations"]["pickup"].pop("lon")
    s = farm_in.summarize(p)
    assert s.timezone == "America/New_York" and s.timezone_assumed is True
    assert s.pickup_at == datetime(2024, 8, 3, 14, 27, 22, tzinfo=UTC)
    assert farm_in.ALERT_TZ_ASSUMED in s.alerts


def test_summarize_missing_pickup_time():
    p = sample_payload()
    p["locations"]["pickup"].pop("time")
    s = farm_in.summarize(p)
    assert s.pickup_at is None and farm_in.ALERT_NO_PICKUP in s.alerts


def test_hourly_run_type():
    p = sample_payload()
    p["runType"] = "Hourly"
    assert farm_in.summarize(p).hourly is True


@pytest.mark.parametrize(
    "rate,transfer_min,hourly_min,hourly,expected",
    [
        (Decimal("165"), Decimal("1"), Decimal("4"), False, Decimal("165.00")),
        (Decimal("165"), Decimal("1"), Decimal("4"), True, Decimal("660.00")),
        (Decimal("0"), Decimal("1"), Decimal("4"), False, None),
        (Decimal("165"), Decimal("1"), Decimal("0"), True, None),
    ],
)
def test_price_request(rate, transfer_min, hourly_min, hourly, expected):
    vt = VehicleTypeFactory(name="Luxury Sedan")
    # The factory is get_or_create on name and returns the seeded row, so set pricing explicitly.
    VehicleType.objects.filter(pk=vt.pk).update(
        rate=rate, transfer_min_hours=transfer_min, hourly_min_hours=hourly_min
    )
    vt.refresh_from_db()
    assert farm_in.price_request(vt, hourly=hourly) == expected


def test_price_request_no_vehicle():
    assert farm_in.price_request(None, hourly=False) is None
