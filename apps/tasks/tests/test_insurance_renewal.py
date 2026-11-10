"""APC-71 — affiliate insurance renewal as owned, vendor-level tasks."""

from contextlib import contextmanager
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.notifications.models import Notification
from apps.tasks import vendor_tasks
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task
from apps.vendors import compliance
from apps.vendors.factories import VendorFactory, VendorInsuranceFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@contextmanager
def _at(moment):
    with patch("django.utils.timezone.now", return_value=moment):
        yield


def _local(day, clock=time(0, 0)):
    return datetime.combine(day, clock, tzinfo=timezone.get_current_timezone())


def _policy(days_left, **kwargs):
    return VendorInsuranceFactory(
        expiry_date=timezone.localdate() + timedelta(days=days_left), **kwargs
    )


def _task(policy):
    return Task.objects.get(insurance=policy, kind="insurance_renewal")


def test_a_policy_gets_a_task_that_opens_at_t_minus_30_and_is_due_at_expiry():
    policy = _policy(60)

    run_tasks()

    task = _task(policy)
    assert task.vendor == policy.vendor
    assert task.lead is None and task.reservation is None
    assert task.department == "affiliate_mgmt"
    assert task.status == Task.Status.SCHEDULED
    assert task.opens_at == _local(policy.expiry_date - timedelta(days=30))
    assert task.due_at == _local(policy.expiry_date, time(9, 0))


def test_it_opens_on_the_tick_after_t_minus_30():
    policy = _policy(60)
    run_tasks()

    with _at(_local(policy.expiry_date - timedelta(days=30), time(0, 5))):
        run_tasks()

    assert _task(policy).status == Task.Status.OPEN


def test_a_policy_already_inside_the_window_opens_straight_away():
    policy = _policy(12)

    run_tasks()

    assert _task(policy).status == Task.Status.OPEN


def test_uploading_a_later_policy_does_not_close_it(client):
    """TASK-10: an expiring policy is still critical while another is valid — the vendor
    can't operate once any policy on file has lapsed, so its renewal stays owned work."""
    policy = _policy(12)
    run_tasks()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    renewal = {
        "insurer": "Acme Mutual",
        "policy_number": "P-NEW",
        "coverage_amount": "1000000",
        "effective_date": policy.expiry_date.isoformat(),
        "expiry_date": (policy.expiry_date + timedelta(days=365)).isoformat(),
    }

    client.post(reverse("insurance_create", args=[policy.vendor.pk]), renewal)

    assert _task(policy).status == Task.Status.OPEN
    # The new policy is a year out, so its own task is only scheduled.
    new = policy.vendor.policies.get(policy_number="P-NEW")
    assert _task(new).status == Task.Status.SCHEDULED


def test_a_policy_expiring_in_10_days_is_critical_and_gets_a_task_while_another_is_valid():
    soon = _policy(10)
    valid = _policy(365, vendor=soon.vendor)

    run_tasks()

    assert _task(soon).status == Task.Status.OPEN
    assert _task(valid).status == Task.Status.SCHEDULED
    (row,) = compliance.report()
    assert (row.status, row.expiry) == ("critical", soon.expiry_date)


def test_moving_the_policys_expiry_a_year_out_closes_its_task(client):
    policy = _policy(10)
    _policy(365, vendor=policy.vendor)
    run_tasks()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    renewed = {
        "insurer": policy.insurer,
        "policy_number": policy.policy_number,
        "coverage_amount": "1000000",
        "effective_date": policy.effective_date.isoformat(),
        "expiry_date": (policy.expiry_date + timedelta(days=365)).isoformat(),
    }

    client.post(reverse("insurance_edit", args=[policy.pk]), renewed)

    task = _task(policy)
    assert task.status == Task.Status.DONE
    assert task.completed_by is None


def test_adding_an_unrelated_policy_does_not_close_it():
    policy = _policy(10)
    run_tasks()
    _policy(400, vendor=policy.vendor)

    run_tasks()

    assert _task(policy).status == Task.Status.OPEN


def test_a_renewed_policys_task_comes_back_when_its_new_expiry_comes_round():
    policy = _policy(10)
    run_tasks()
    policy.expiry_date += timedelta(days=365)
    policy.save()
    run_tasks()
    Task.objects.filter(insurance=policy).update(escalated_tier=2)

    with _at(_local(policy.expiry_date - timedelta(days=30), time(0, 5))):
        run_tasks()

    task = _task(policy)
    assert task.status == Task.Status.OPEN
    assert task.completed_at is None
    assert task.escalated_tier == 0
    assert task.opens_at == _local(policy.expiry_date - timedelta(days=30))
    assert task.due_at == _local(policy.expiry_date, time(9, 0))


def test_a_task_the_old_superseded_rule_closed_reopens():
    policy = _policy(10)
    _policy(365, vendor=policy.vendor)
    run_tasks()
    Task.objects.filter(insurance=policy).update(
        status=Task.Status.DONE, completed_at=timezone.now(), completed_by=None
    )

    run_tasks()

    assert _task(policy).status == Task.Status.OPEN


def test_a_renewal_a_person_closed_stays_closed():
    policy = _policy(10)
    run_tasks()
    Task.objects.filter(insurance=policy).update(
        status=Task.Status.DONE, completed_at=timezone.now(), completed_by=UserFactory()
    )

    run_tasks()

    assert _task(policy).status == Task.Status.DONE


def test_moving_the_expiry_inside_the_window_re_dates_the_open_task():
    policy = _policy(20)
    run_tasks()
    opened = _task(policy).opens_at
    policy.expiry_date -= timedelta(days=5)
    policy.save()

    run_tasks()

    task = _task(policy)
    assert task.status == Task.Status.OPEN
    assert task.opens_at == opened
    assert task.due_at == _local(policy.expiry_date, time(9, 0))


def test_moving_the_expiry_re_dates_a_scheduled_task():
    policy = _policy(60)
    run_tasks()
    policy.expiry_date += timedelta(days=30)
    policy.save()

    run_tasks()

    task = _task(policy)
    assert task.status == Task.Status.SCHEDULED
    assert task.opens_at == _local(policy.expiry_date - timedelta(days=30))
    assert task.due_at == _local(policy.expiry_date, time(9, 0))


def test_a_policy_with_the_same_or_earlier_expiry_doesnt_close_it():
    policy = _policy(12)
    run_tasks()
    _policy(12, vendor=policy.vendor)
    _policy(-40, vendor=policy.vendor)

    run_tasks()

    assert _task(policy).status == Task.Status.OPEN


def test_a_lapsed_policy_gets_its_task_even_with_a_later_one_on_file():
    """TASK-10: a lapsed policy on file means the vendor can't operate (Compliance shows
    Expired), so it is chased until it is renewed or removed."""
    old = _policy(-200)
    _policy(160, vendor=old.vendor)

    run_tasks()

    assert _task(old).status == Task.Status.OPEN


def test_an_inactive_vendor_gets_no_task():
    policy = _policy(12, vendor=VendorFactory(status=Vendor.Status.INACTIVE))

    run_tasks()

    assert not Task.objects.filter(vendor=policy.vendor).exists()


def test_deactivating_a_vendor_marks_its_open_tasks_not_applicable(client):
    policy = _policy(12)
    run_tasks()
    vendor = policy.vendor
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    client.post(
        reverse("vendor_edit", args=[vendor.pk]),
        {
            "name": vendor.name,
            "phone": vendor.phone,
            "email": vendor.email,
            "status": Vendor.Status.INACTIVE,
        },
    )

    vendor.refresh_from_db()
    assert vendor.status == Vendor.Status.INACTIVE
    assert _task(policy).status == Task.Status.NOT_APPLICABLE


def test_the_tick_catches_a_deactivation_no_hook_saw():
    policy = _policy(12)
    run_tasks()
    Vendor.objects.filter(pk=policy.vendor.pk).update(status=Vendor.Status.INACTIVE)

    run_tasks()

    assert _task(policy).status == Task.Status.NOT_APPLICABLE


def test_ticks_are_idempotent():
    _policy(12)
    _policy(90)
    run_tasks()
    snapshot = sorted(Task.objects.values_list("pk", "status", "opens_at", "due_at", "updated_at"))

    assert run_tasks() == 0
    rows = sorted(Task.objects.values_list("pk", "status", "opens_at", "due_at", "updated_at"))
    assert rows == snapshot


def test_a_task_belongs_to_an_order_or_a_vendor_never_both():
    policy = _policy(12)
    run_tasks()
    task = _task(policy)

    with pytest.raises(IntegrityError), transaction.atomic():
        Task.objects.filter(pk=task.pk).update(lead=LeadFactory())


def test_an_overdue_renewal_escalates_with_the_vendor_named(client):
    admin = UserFactory(role=User.Role.OWNER_ADMIN)
    policy = _policy(1)
    run_tasks()

    with _at(_local(policy.expiry_date, time(10, 0))):
        run_tasks()

    note = Notification.objects.get(kind=Notification.Kind.TASK_OVERDUE)
    assert note.lead is None and note.vendor == policy.vendor
    assert policy.vendor.name in note.detail
    assert note.url == reverse("vendor_detail", args=[policy.vendor.pk])
    client.force_login(admin)
    assert client.get(reverse("dashboard")).status_code == 200


def test_the_queue_shows_the_vendor_where_an_order_would_be(client):
    owner = UserFactory()
    policy = _policy(5)
    run_tasks()
    Task.objects.filter(insurance=policy).update(assignee=owner)
    client.force_login(owner)

    body = client.get(reverse("task_queue")).content.decode()

    assert policy.vendor.name in body
    assert reverse("vendor_detail", args=[policy.vendor.pk]) in body


def test_vendor_task_times_display_in_the_business_zone():
    policy = _policy(12)
    run_tasks()

    assert _task(policy).tz_name == settings.TIME_ZONE


def test_sync_vendor_is_the_hook():
    policy = _policy(12)

    vendor_tasks.sync_vendor(policy.vendor)

    assert _task(policy).status == Task.Status.OPEN


def test_a_new_renewal_task_is_never_born_overdue_whatever_the_schedule():
    from apps.tasks.models import TaskSchedule

    TaskSchedule.objects.create(
        kind="insurance_renewal", variant="standard", amount=45, unit="days"
    )
    policy = _policy(60)

    run_tasks()

    task = _task(policy)
    assert task.due_at == task.opens_at
    vendor_tasks.redate()
    task.refresh_from_db()
    assert task.due_at == task.opens_at
