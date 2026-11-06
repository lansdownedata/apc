from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import transaction

from apps.contacts.factories import CompanyFactory
from apps.contacts.models import Company, Contact
from apps.core.choices import Channel
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


@pytest.mark.parametrize(
    "pickup",
    [
        {"locationType": "airport", "address": "LAX", "time": "2024-08-03T10:27:22"},
        {"address": " lax ", "time": "2024-08-03T10:27:22"},
    ],
)
def test_summarize_airport_pickup_uses_airport_zone(settings, pickup):
    settings.TIME_ZONE = "America/New_York"
    p = sample_payload()
    p["locations"]["pickup"] = pickup
    s = farm_in.summarize(p)
    assert s.timezone == "America/Los_Angeles" and s.timezone_assumed is False
    assert s.pickup_at == datetime(2024, 8, 3, 17, 27, 22, tzinfo=UTC)
    assert farm_in.ALERT_TZ_ASSUMED not in s.alerts


def test_summarize_unknown_airport_code_assumes_default_zone(settings):
    settings.TIME_ZONE = "America/New_York"
    p = sample_payload()
    p["locations"]["pickup"] = {"locationType": "airport", "address": "QQQ", "time": "x"}
    s = farm_in.summarize(p)
    assert s.timezone == "America/New_York" and s.timezone_assumed is True


def test_summarize_pickup_time_out_of_range():
    p = sample_payload()
    p["locations"]["pickup"]["time"] = "9999-12-31T23:59:59"
    s = farm_in.summarize(p)
    assert s.pickup_at is None and farm_in.ALERT_NO_PICKUP in s.alerts


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


@pytest.mark.parametrize(
    "raw,expected",
    [("two", 1), ("2.0", 1), ("2+1", 1), ("0", 1), ("-3", 1), ({"n": 2}, 1), ([2], 1), (" 3 ", 3)],
)
def test_passenger_count_is_tolerant(raw, expected):
    p = sample_payload()
    p["passengerCount"] = raw
    assert farm_in.summarize(p).passenger_count == expected


def test_malformed_locations_do_not_raise():
    p = sample_payload()
    p["locations"] = "somewhere"
    s = farm_in.summarize(p)
    assert s.pickup_at is None and farm_in.ALERT_NO_PICKUP in s.alerts
    assert s.pickup_summary == "" and s.dropoff_summary == ""


def test_malformed_passengers_do_not_raise():
    p = sample_payload()
    p["passengers"] = ["Alex"]
    s = farm_in.summarize(p)
    assert s.passenger_name == ""
    p["passengers"] = "Alex"
    assert farm_in.summarize(p).passenger_name == ""


def test_ensure_requester_creates_company_and_contact():
    with transaction.atomic():
        company, created = farm_in.ensure_requester(sample_payload())
    assert created and company.gnet_grid_id == "gnettest" and company.is_affiliate_account
    contact = Contact.objects.get(company=company)
    assert contact.name == "gnettest" and contact.channel == Channel.GNET


def test_ensure_requester_reuses_by_grid_id():
    existing = CompanyFactory(name="GNet Test Operator", gnet_grid_id="gnettest")
    with transaction.atomic():
        company, created = farm_in.ensure_requester(sample_payload())
    assert company == existing and not created and Company.objects.count() == 1


def test_ensure_requester_adopts_same_named_company():
    existing = CompanyFactory(name="gnettest")
    with transaction.atomic():
        company, created = farm_in.ensure_requester(sample_payload())
    existing.refresh_from_db()
    assert company == existing and not created
    assert existing.gnet_grid_id == "gnettest" and existing.is_affiliate_account


def test_ensure_requester_blank_id():
    p = sample_payload()
    p["affiliateReservation"]["requesterId"] = ""
    with pytest.raises(ValueError):
        farm_in.ensure_requester(p)


def test_ensure_requester_too_long():
    p = sample_payload()
    p["affiliateReservation"]["requesterId"] = "a" * 65
    with pytest.raises(ValueError, match="too long"):
        farm_in.ensure_requester(p)


def test_ensure_requester_rejects_non_scalar():
    p = sample_payload()
    p["affiliateReservation"]["requesterId"] = ["a"]
    with pytest.raises(ValueError):
        farm_in.ensure_requester(p)


def test_ensure_requester_int_id():
    p = sample_payload()
    p["affiliateReservation"]["requesterId"] = 42
    with transaction.atomic():
        company, created = farm_in.ensure_requester(p)
    assert created and company.name == "42" and company.gnet_grid_id == "42"


def test_ensure_requester_idempotent():
    with transaction.atomic():
        first, _ = farm_in.ensure_requester(sample_payload())
        second, created = farm_in.ensure_requester(sample_payload())
    assert first == second and not created
    assert Company.objects.count() == 1 and Contact.objects.count() == 1
