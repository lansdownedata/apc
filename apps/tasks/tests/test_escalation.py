"""APC-52 — overdue escalation: tier 1, tier 2, the daily digest, and D2."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.tasks import escalation, services
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig

pytestmark = pytest.mark.django_db

ET = ZoneInfo("America/New_York")


@contextmanager
def _at(moment):
    with patch("django.utils.timezone.now", return_value=moment):
        yield


@pytest.fixture
def config():
    cfg = TaskConfig.load()
    cfg.overdue_grace_hours = 24
    cfg.digest_emails = "ops@allprocharter.com"
    cfg.save()
    return cfg


def _order():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=10),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    services.ensure_tasks(lead)
    return lead


def _overdue(lead, kind, *, hours, assignee=None):
    """Make one task overdue by `hours` and close every other task, so each test sees
    exactly one escalating task."""
    Task.objects.filter(lead=lead).exclude(kind=kind).update(status=Task.Status.NOT_APPLICABLE)
    task = Task.objects.get(lead=lead, kind=kind)
    task.status = Task.Status.OPEN
    task.opens_at = timezone.now() - timedelta(days=5)
    task.due_at = timezone.now() - timedelta(hours=hours)
    task.assignee = assignee
    task.save()
    return task


def _task_notes():
    return Notification.objects.filter(kind=Notification.Kind.TASK_OVERDUE)


def test_overdue_sends_one_tier_one_notification_and_the_next_tick_none(config):
    agent = UserFactory()
    task = _overdue(_order(), "final_itinerary", hours=2, assignee=agent)

    run_tasks()
    run_tasks()

    notes = _task_notes()
    assert notes.count() == 1
    assert notes.get().user == agent
    task.refresh_from_db()
    assert task.escalated_tier == 1


def test_unassigned_tier_one_goes_to_the_department_owner(config):
    owner = UserFactory()
    config.customer_service_owner = owner
    config.save()
    _overdue(_order(), "final_itinerary", hours=2)

    run_tasks()

    assert list(_task_notes().values_list("user", flat=True)) == [owner.pk]


def test_unassigned_with_no_owner_goes_to_admins(config):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    UserFactory(role=User.Role.OWNER_ADMIN, is_active=False)
    _overdue(_order(), "final_itinerary", hours=2)

    run_tasks()

    assert list(_task_notes().values_list("user", flat=True)) == [admin.pk]


def test_past_grace_sends_tier_two_to_the_owner_and_admins_exactly_once(config):
    owner = UserFactory()
    admin_a = UserFactory(role=User.Role.OWNER_ADMIN)
    admin_b = UserFactory(role=User.Role.OWNER_ADMIN)
    config.customer_service_owner = owner
    config.save()
    agent = UserFactory()
    task = _overdue(_order(), "final_itinerary", hours=2, assignee=agent)
    run_tasks()
    assert task.__class__.objects.get(pk=task.pk).escalated_tier == 1

    Task.objects.filter(pk=task.pk).update(due_at=timezone.now() - timedelta(hours=30))
    run_tasks()
    run_tasks()

    tier_two = _task_notes().exclude(user=agent)
    assert set(tier_two.values_list("user", flat=True)) == {owner.pk, admin_a.pk, admin_b.pk}
    assert tier_two.count() == 3
    assert Task.objects.get(pk=task.pk).escalated_tier == 2


def test_the_owner_who_is_also_an_admin_is_told_once(config):
    boss = UserFactory(role=User.Role.OWNER_ADMIN)
    config.customer_service_owner = boss
    config.save()
    _overdue(_order(), "final_itinerary", hours=30, assignee=UserFactory())

    run_tasks()

    assert _task_notes().filter(user=boss).count() == 1


def test_kinds_the_dispatch_monitor_owns_never_notify(config):
    lead = _order()
    for kind in ("affiliate_assigned", "affiliate_confirmed", "driver_info_received"):
        UserFactory(role=User.Role.OWNER_ADMIN)
        task = Task.objects.get(lead=lead, kind=kind)
        task.status = Task.Status.OPEN
        task.due_at = timezone.now() - timedelta(hours=48)
        task.save()
    Task.objects.filter(lead=lead).exclude(
        kind__in=("affiliate_assigned", "affiliate_confirmed", "driver_info_received")
    ).update(status=Task.Status.NOT_APPLICABLE)

    run_tasks()

    assert not _task_notes().exists()
    assert not Task.objects.filter(lead=lead, escalated_tier__gt=0).exists()


def test_a_task_that_is_not_overdue_does_not_escalate(config):
    UserFactory(role=User.Role.OWNER_ADMIN)
    _overdue(_order(), "final_itinerary", hours=-5)

    run_tasks()

    assert not _task_notes().exists()


@override_settings(COMPANY_EMAIL="office@allprocharter.com")
def test_the_digest_sends_once_per_business_day_grouped_by_department(config):
    UserFactory(role=User.Role.OWNER_ADMIN)
    lead = _order()
    Task.objects.filter(lead=lead).update(status=Task.Status.NOT_APPLICABLE)
    morning = datetime.combine(timezone.localdate(), time(escalation.DIGEST_HOUR, 5), tzinfo=ET)
    for kind in ("final_itinerary", "final_balance_paid"):
        Task.objects.filter(lead=lead, kind=kind).update(
            status=Task.Status.OPEN,
            opens_at=morning - timedelta(days=5),
            due_at=morning - timedelta(hours=3),
        )

    with _at(morning):
        run_tasks()
    with _at(morning + timedelta(hours=6)):
        run_tasks()

    digests = [m for m in mail.outbox if "overdue" in m.subject.lower()]
    assert len(digests) == 1
    assert digests[0].to == ["ops@allprocharter.com"]
    body = digests[0].body
    assert body.index("Accounting") < body.index("Final balance paid")
    assert body.index("Customer Service") < body.index("Final itinerary received")

    with _at(morning + timedelta(days=1)):
        run_tasks()
    assert len([m for m in mail.outbox if "overdue" in m.subject.lower()]) == 2


def test_no_digest_before_the_morning_hour_or_with_nothing_overdue(config):
    lead = _order()
    Task.objects.filter(lead=lead).update(status=Task.Status.NOT_APPLICABLE)
    early = datetime.combine(timezone.localdate(), time(2, 0), tzinfo=ET)

    with _at(early + timedelta(hours=escalation.DIGEST_HOUR)):
        run_tasks()
    Task.objects.filter(lead=lead, kind="final_itinerary").update(
        status=Task.Status.OPEN, due_at=early - timedelta(hours=5)
    )
    with _at(early):
        run_tasks()

    assert not [m for m in mail.outbox if "overdue" in m.subject.lower()]
