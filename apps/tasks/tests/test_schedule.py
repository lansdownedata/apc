"""Settings-owned checklist timing: `TaskSchedule` rows layered over the code defaults."""

import importlib
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import schedule, services, vendor_tasks
from apps.tasks.definitions import (
    KINDS,
    AfterLastPickup,
    AfterOpen,
    BeforePickup,
    default_timing,
    effective_kind,
)
from apps.tasks.facts import load_facts
from apps.tasks.models import Task, TaskSchedule
from apps.vendors.factories import VendorInsuranceFactory

pytestmark = pytest.mark.django_db
NY = ZoneInfo("America/New_York")
Std, Farm = TaskSchedule.Variant.STANDARD, TaskSchedule.Variant.FARM_IN
Unit = TaskSchedule.Unit


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(*, farm_in: bool = False, trips: int = 1):
    company = CompanyFactory(is_affiliate_account=farm_in)
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(company=company))
    day = timezone.localdate() + timedelta(days=20)
    for i in range(trips):
        ReservationFactory(
            lead=lead,
            pickup_timezone="America/New_York",
            hours=Decimal("1"),
            pickup_date=day,
            pickup_time=time(9 + i, 0),
        )
    return lead


def _row(kind, variant, amount, unit):
    return TaskSchedule.objects.create(kind=kind, variant=variant, amount=amount, unit=unit)


def _effective(lead, key):
    return effective_kind(KINDS[key], load_facts([lead])[lead.pk])


def _pickup(lead):
    return lead.reservations.get().pickup_at


def test_no_rows_matches_code_default():
    lead = _order()

    assert _effective(lead, "affiliate_assigned").due == BeforePickup(days=3)


def test_standard_row_changes_due():
    lead = _order()
    _row("affiliate_assigned", Std, 5, Unit.DAYS)

    due = _effective(lead, "affiliate_assigned").due

    assert due == BeforePickup(days=5, day_start=False)
    assert due.resolve(_pickup(lead)) == (_pickup(lead) - timedelta(days=5)).astimezone(UTC)


def test_days_keep_start_of_day():
    lead = _order()
    _row("wedding_names", Std, 10, Unit.DAYS)

    due = _effective(lead, "wedding_names").due

    day = _pickup(lead).date() - timedelta(days=10)
    expected = datetime.combine(day, time(0, 0), tzinfo=NY).astimezone(UTC)
    assert due.resolve(_pickup(lead)) == expected


def test_hours_on_start_of_day_item_is_exact():
    lead = _order()
    _row("wedding_names", Std, 36, Unit.HOURS)

    due = _effective(lead, "wedding_names").due

    assert due == BeforePickup(hours=36, day_start=False)
    assert due.resolve(_pickup(lead)) == (_pickup(lead) - timedelta(hours=36)).astimezone(UTC)


def test_farm_in_row_applies_only_to_farm_in():
    farm, plain = _order(farm_in=True), _order(farm_in=False)
    _row("deposit_received", Farm, 5, Unit.DAYS)

    assert _effective(farm, "deposit_received").due == BeforePickup(days=5, day_start=True)
    assert _effective(plain, "deposit_received").due == AfterOpen(hours=72)


def test_standard_row_does_not_touch_farm_in_override():
    farm = _order(farm_in=True)
    _row("deposit_received", Std, 96, Unit.HOURS)

    assert _effective(farm, "deposit_received").due == BeforePickup(days=3, day_start=True)


def test_row_for_unknown_kind_is_ignored():
    lead = _order()
    _row("gone", Std, 3, Unit.DAYS)

    load_facts([lead])
    services.ensure_tasks(lead)

    assert Task.objects.filter(lead=lead).exists()


def test_future_booking_follow_up_reads_days():
    last = datetime(2026, 11, 2, 15, 0, tzinfo=NY)

    due = AfterLastPickup(days=30).resolve(last)

    assert due == datetime(2026, 12, 2, 9, 0, tzinfo=NY).astimezone(UTC)


def test_insurance_renewal_due_follows_row():
    _row("insurance_renewal", Std, 7, Unit.DAYS)
    policy = VendorInsuranceFactory(expiry_date=timezone.localdate() + timedelta(days=60))

    vendor_tasks.run()

    task = Task.objects.get(insurance=policy, kind="insurance_renewal")
    day = policy.expiry_date - timedelta(days=7)
    local = datetime.combine(day, time(9, 0), tzinfo=timezone.get_current_timezone())
    assert task.due_at == local.astimezone(UTC)


def test_default_timing_values():
    expected = {
        "deposit_received": (72, "hours"),
        "wedding_names": (7, "days"),
        "driver_released": (12, "hours"),
        "affiliate_paid": (14, "days"),
        "planner_change_request": (1, "business_days"),
        "future_booking_followup": (330, "days"),
    }

    assert {key: default_timing(KINDS[key]) for key in expected} == expected


def test_facts_query_count_is_fixed():
    one, many = _order(trips=1), _order(trips=10)
    with CaptureQueriesContext(connection) as few:
        load_facts([one])
    with CaptureQueriesContext(connection) as lots:
        load_facts([many])

    assert len(few) == len(lots)


def test_load_is_one_query():
    _row("wedding_names", Std, 10, Unit.DAYS)

    with CaptureQueriesContext(connection) as ctx:
        loaded = schedule.load()

    assert len(ctx) == 1
    assert loaded == {("wedding_names", "standard"): (10, "days")}


def test_move_future_booking_offset():
    migration = importlib.import_module("apps.tasks.migrations.0014_move_future_booking_offset")

    assert migration.schedule_row_for(360) == {
        "kind": "future_booking_followup",
        "variant": "standard",
        "amount": 360,
        "unit": "days",
    }
    assert migration.schedule_row_for(330) is None
