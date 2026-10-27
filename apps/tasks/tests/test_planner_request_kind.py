"""APC-68 — the planner change-request task kind: opened by a request, never generated."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import services
from apps.tasks.definitions import KINDS, AfterOpenBusinessDays, Level
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("opened", "due"),
    [
        (datetime(2026, 10, 26, 10, 0, tzinfo=NY), datetime(2026, 10, 27, 10, 0, tzinfo=NY)),  # Mon
        (datetime(2026, 10, 30, 15, 0, tzinfo=NY), datetime(2026, 11, 2, 15, 0, tzinfo=NY)),  # Fri
        (datetime(2026, 10, 31, 9, 0, tzinfo=NY), datetime(2026, 11, 2, 9, 0, tzinfo=NY)),  # Sat
        (datetime(2026, 11, 1, 9, 0, tzinfo=NY), datetime(2026, 11, 2, 9, 0, tzinfo=NY)),  # Sun
    ],
)
def test_one_business_day_skips_the_weekend(settings, opened, due):
    settings.TIME_ZONE = "America/New_York"
    assert AfterOpenBusinessDays(days=1).resolve_from(opened) == due


def test_the_kind_is_customer_service_order_level_due_in_a_business_day():
    kind = KINDS["planner_change_request"]
    assert kind.department == "customer_service"
    assert kind.level == Level.ORDER
    assert kind.due == AfterOpenBusinessDays(days=1)


def test_generation_never_creates_it():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, pickup_date=timezone.localdate() + timedelta(days=20))
    services.ensure_tasks(lead)
    assert not Task.objects.filter(kind="planner_change_request").exists()


def test_open_request_task_opens_one_owned_task_each_time():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, pickup_date=timezone.localdate() + timedelta(days=20))
    first = services.open_request_task(lead, "planner_change_request")
    second = services.open_request_task(lead, "planner_change_request")
    assert first.pk != second.pk
    assert first.status == Task.Status.OPEN
    assert first.reservation is None
    assert first.department == "customer_service"
    assert first.due_at > first.opens_at
    # rescheduling the order leaves a request's due date where it was
    services.reschedule(lead)
    first.refresh_from_db()
    assert first.due_at == AfterOpenBusinessDays(days=1).resolve_from(first.opens_at)


def test_a_new_request_follows_the_saved_checklist_schedule(settings):
    from apps.tasks.models import TaskSchedule

    settings.TIME_ZONE = "America/New_York"
    TaskSchedule.objects.create(
        kind="planner_change_request", variant="standard", amount=5, unit="business_days"
    )
    lead = LeadFactory(status=Lead.Status.BOOKED)
    task = services.open_request_task(lead, "planner_change_request")
    assert task.due_at == AfterOpenBusinessDays(days=5).resolve_from(task.opens_at)
