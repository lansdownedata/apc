"""Saving Settings schedule rows re-dates open work."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import schedule, services, vendor_tasks
from apps.tasks.models import Task, TaskSchedule
from apps.vendors.factories import VendorInsuranceFactory

pytestmark = pytest.mark.django_db
Std, Farm = TaskSchedule.Variant.STANDARD, TaskSchedule.Variant.FARM_IN
KEY = ("affiliate_assigned", Std)


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(*, farm_in: bool = False):
    company = CompanyFactory(is_affiliate_account=farm_in)
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(company=company))
    ReservationFactory(
        lead=lead,
        pickup_timezone="America/New_York",
        hours=Decimal("1"),
        pickup_date=timezone.localdate() + timedelta(days=20),
        pickup_time=time(9, 0),
    )
    services.ensure_tasks(lead)
    return lead


def _task(lead, kind):
    return Task.objects.get(lead=lead, kind=kind)


def _pickup(lead):
    return lead.reservations.get().pickup_at


def test_save_default_value_deletes_row():
    TaskSchedule.objects.create(kind=KEY[0], variant=Std, amount=5, unit="days")

    schedule.save({KEY: (3, "days")}, user=UserFactory())

    assert not TaskSchedule.objects.exists()


def test_save_upserts_with_user():
    user = UserFactory()

    schedule.save({KEY: (5, "days")}, user=user)
    schedule.save({KEY: (6, "days")}, user=user)

    row = TaskSchedule.objects.get()
    assert (row.amount, row.unit, row.updated_by) == (6, "days", user)


def test_save_redates_unresolved_only():
    lead = _order()
    ReservationFactory(
        lead=lead,
        pickup_timezone="America/New_York",
        pickup_date=timezone.localdate() + timedelta(days=21),
        pickup_time=time(9, 0),
    )
    ReservationFactory(
        lead=lead,
        pickup_timezone="America/New_York",
        pickup_date=timezone.localdate() + timedelta(days=22),
        pickup_time=time(9, 0),
    )
    services.ensure_tasks(lead)
    opened, scheduled, closed = Task.objects.filter(lead=lead, kind="affiliate_assigned")
    Task.objects.filter(pk=opened.pk).update(status=Task.Status.OPEN)
    Task.objects.filter(pk=scheduled.pk).update(status=Task.Status.SCHEDULED)
    Task.objects.filter(pk=closed.pk).update(status=Task.Status.DONE)
    before = Task.objects.get(pk=closed.pk).due_at

    count = schedule.save({KEY: (5, "days")}, user=UserFactory())

    assert count == 2
    assert Task.objects.get(pk=closed.pk).due_at == before
    for task in (opened, scheduled):
        task = Task.objects.get(pk=task.pk)
        pickup = task.reservation.pickup_at
        assert task.due_at == (pickup - timedelta(days=5)).astimezone(UTC)


def test_open_task_keeps_its_open_time():
    lead = _order()
    task = _task(lead, "affiliate_assigned")
    Task.objects.filter(pk=task.pk).update(status=Task.Status.OPEN)
    opens = Task.objects.get(pk=task.pk).opens_at

    schedule.save({KEY: (5, "days")}, user=UserFactory())

    assert Task.objects.get(pk=task.pk).opens_at == opens


def test_escalated_tier_untouched():
    lead = _order()
    task = _task(lead, "affiliate_assigned")
    Task.objects.filter(pk=task.pk).update(status=Task.Status.OPEN, escalated_tier=1)

    schedule.save({KEY: (5, "days")}, user=UserFactory())

    assert Task.objects.get(pk=task.pk).escalated_tier == 1


def test_due_never_before_open():
    lead = _order()
    task = _task(lead, "deposit_received")
    opened = timezone.now() - timedelta(days=1)
    Task.objects.filter(pk=task.pk).update(status=Task.Status.OPEN, opens_at=opened)

    schedule.save({("deposit_received", Std): (0, "hours")}, user=UserFactory())

    fresh = Task.objects.get(pk=task.pk)
    assert fresh.due_at == fresh.opens_at == opened


def test_unchanged_keys_redate_nothing():
    TaskSchedule.objects.create(kind=KEY[0], variant=Std, amount=5, unit="days")
    _order()

    with patch.object(services, "reschedule") as spy:
        count = schedule.save({KEY: (5, "days"), ("wedding_names", Std): (7, "days")}, user=None)

    assert count == 0
    spy.assert_not_called()


def test_farm_in_key_redates_farm_in_order():
    lead = _order(farm_in=True)
    task = _task(lead, "deposit_received")

    schedule.save({("deposit_received", Farm): (5, "days")}, user=UserFactory())

    day = _pickup(lead).astimezone(ZoneInfo("America/New_York")).date() - timedelta(days=5)
    expected = datetime.combine(day, time(0, 0), tzinfo=ZoneInfo("America/New_York"))
    assert Task.objects.get(pk=task.pk).due_at == expected.astimezone(UTC)


def test_insurance_renewal_redated():
    policy = VendorInsuranceFactory(expiry_date=timezone.localdate() + timedelta(days=60))
    vendor_tasks.run()

    count = schedule.save({("insurance_renewal", Std): (14, "days")}, user=UserFactory())

    task = Task.objects.get(insurance=policy, kind="insurance_renewal")
    day = policy.expiry_date - timedelta(days=14)
    local = datetime.combine(day, time(9, 0), tzinfo=timezone.get_current_timezone())
    assert task.due_at == local.astimezone(UTC)
    assert count == 1
