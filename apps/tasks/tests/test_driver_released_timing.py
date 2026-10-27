"""The "Driver info released" checklist task follows the Driver details message's timing."""

import importlib
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import overrides
from apps.messaging.models import MessageTemplate
from apps.reservations.factories import ReservationFactory
from apps.tasks import schedule, services
from apps.tasks.models import Task, TaskSchedule

pytestmark = pytest.mark.django_db
KEY = ("driver_released", "standard")
migration = importlib.import_module("apps.tasks.migrations.0015_driver_released_follows_message")


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_timezone="America/New_York",
        hours=Decimal("1"),
        pickup_date=timezone.localdate() + timedelta(days=20),
        pickup_time=time(9, 0),
    )
    services.ensure_tasks(lead)
    return lead, trip


def _task(lead):
    return Task.objects.get(lead=lead, kind="driver_released")


def test_task_due_follows_message_default():
    lead, trip = _order()
    assert _task(lead).due_at == trip.pickup_at - timedelta(hours=11)


def test_task_due_follows_message_row():
    MessageTemplate.objects.create(kind="driver_released", timing_amount=6, timing_unit="hours")
    lead, trip = _order()
    assert _task(lead).due_at == trip.pickup_at - timedelta(hours=5)


def test_task_due_follows_message_row_in_minutes():
    MessageTemplate.objects.create(kind="driver_released", timing_amount=90, timing_unit="minutes")
    lead, trip = _order()
    assert _task(lead).due_at == trip.pickup_at - timedelta(minutes=30)


def test_schedule_row_is_ignored():
    TaskSchedule.objects.create(kind="driver_released", variant="standard", amount=3, unit="days")
    lead, trip = _order()
    assert _task(lead).due_at == trip.pickup_at - timedelta(hours=11)


def test_saving_message_timing_redates_open_tasks():
    lead, trip = _order()
    base = overrides.default("driver_released")
    count = overrides.save(
        "driver_released",
        subject=base.subject,
        email_body=base.email_body,
        sms_body=base.sms_body,
        timing=(4, "hours"),
        enabled=True,
        user=UserFactory(),
    )
    assert count >= 1
    assert _task(lead).due_at == trip.pickup_at - timedelta(hours=3)
    overrides.reset("driver_released")
    assert _task(lead).due_at == trip.pickup_at - timedelta(hours=11)


def test_schedule_save_ignores_driver_released():
    schedule.save({KEY: (3, "days")}, user=UserFactory())
    assert not TaskSchedule.objects.filter(kind="driver_released").exists()


def test_schedule_page_row_readonly(client):
    client.force_login(UserFactory(role="owner_admin"))
    body = client.get(reverse("checklist_schedule")).content.decode()
    assert "Set on the Driver details message (due an hour after it sends)" in body
    assert "due Sat" in body and "8:30 AM" in body
    assert reverse("message_edit", args=["driver_released"]) in body
    assert "amount__driver_released__standard" not in body
    assert "unit__driver_released__standard" not in body

    data = {}
    for row in schedule.ROWS:
        amount, unit = schedule.code_default(row.key, row.variant)
        data[f"amount__{row.key}__{row.variant}"] = amount
        data[f"unit__{row.key}__{row.variant}"] = unit
    data["amount__driver_released__standard"] = 3
    data["unit__driver_released__standard"] = "days"
    assert client.post(reverse("checklist_schedule"), data).status_code == 302
    assert not TaskSchedule.objects.filter(kind="driver_released").exists()


@pytest.mark.parametrize(
    ("amount", "unit", "expected"),
    [
        (12, "hours", None),
        (6, "hours", (6, "hours")),
        (2, "days", (48, "hours")),
        (30, "hours", (30, "hours")),
        (0, "hours", (1, "hours")),
    ],
)
def test_migration_moves_existing_row(amount, unit, expected):
    assert migration.message_timing_for(amount, unit) == expected
