"""APC-51 — the run-tasks cron: open, re-evaluate, reschedule, cancel."""

from contextlib import contextmanager
from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.core.cron import JOBS
from apps.dispatch import services as dispatch
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.drafts import save_reservation_from_draft
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.tasks import services
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig

pytestmark = pytest.mark.django_db


@contextmanager
def _at(moment):
    with patch("django.utils.timezone.now", return_value=moment):
        yield


def _order(trips=1, days=60):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    for i in range(trips):
        ReservationFactory(
            lead=lead,
            pickup_date=timezone.localdate() + timedelta(days=days + i),
            pickup_time=time(9, 0),
            pickup_timezone="America/New_York",
        )
    services.ensure_tasks(lead)
    return lead


def _task(lead, kind, reservation=None):
    return Task.objects.get(lead=lead, kind=kind, reservation=reservation)


def test_run_tasks_is_registered_as_a_cron_job():
    assert JOBS["run-tasks"] is run_tasks


def test_a_scheduled_task_opens_on_the_first_tick_after_opens_at():
    lead = _order()
    task = _task(lead, "final_balance_paid")
    assert task.status == Task.Status.SCHEDULED

    with _at(task.opens_at - timedelta(minutes=1)):
        run_tasks()
    task.refresh_from_db()
    assert task.status == Task.Status.SCHEDULED

    with _at(task.opens_at + timedelta(minutes=1)):
        run_tasks()
    task.refresh_from_db()
    assert task.status == Task.Status.OPEN


def test_a_predicate_satisfied_through_an_unhooked_path_closes_on_the_next_tick():
    lead = _order()
    trip = lead.reservations.get()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    services.ensure_tasks(lead)
    assert _task(lead, "affiliate_confirmed", trip).status == Task.Status.OPEN

    Assignment.objects.filter(pk=a.pk).update(
        status=Assignment.Status.CONFIRMED, affiliate_confirmed_at=timezone.now()
    )
    run_tasks()

    assert _task(lead, "affiliate_confirmed", trip).status == Task.Status.DONE


def test_moving_pickup_redates_open_tasks_and_leaves_done_ones_alone():
    user = UserFactory()
    lead = _order()
    trip = lead.reservations.get()
    open_task = _task(lead, "driver_assigned", trip)
    done_task = _task(lead, "affiliate_assigned", trip)
    services.complete(done_task, user=user)
    done_due = done_task.due_at

    payload = {
        "trip_type": "transfer",
        "date": (trip.pickup_date + timedelta(days=2)).isoformat(),
        "time": "09:00",
        "passengers": 2,
        "vehicle_id": trip.vehicle_id,
        "rate": "185",
        "hours": "1",
        "min_hours": "0",
        "stops": [{"address": "Pickup"}, {"address": "Drop-off"}],
    }
    save_reservation_from_draft(lead, payload, instance=trip)

    open_task_after = _task(lead, "driver_assigned", trip)
    assert open_task_after.due_at - open_task.due_at == timedelta(days=2)
    done_task.refresh_from_db()
    assert done_task.due_at == done_due


def test_cancelling_a_trip_marks_only_its_open_tasks_not_applicable():
    user = UserFactory()
    lead = _order(trips=2)
    keep, cancel = lead.reservations.order_by("pickup_date")
    done = _task(lead, "affiliate_assigned", cancel)
    services.complete(done, user=user)

    cancel.trip_status = Reservation.TripStatus.CANCELLED
    cancel.save(update_fields=["trip_status"])
    dispatch.release_trips([cancel], note="Cancelled")

    cancelled = Task.objects.filter(reservation=cancel)
    assert set(cancelled.exclude(pk=done.pk).values_list("status", flat=True)) == {
        Task.Status.NOT_APPLICABLE
    }
    done.refresh_from_db()
    assert done.status == Task.Status.DONE
    assert not Task.objects.filter(reservation=keep, status=Task.Status.NOT_APPLICABLE).exists()
    assert _task(lead, "final_itinerary").status == Task.Status.OPEN


def test_cancelling_the_order_marks_all_its_open_tasks_not_applicable():
    lead = _order(trips=2)
    lead.reservations.update(trip_status=Reservation.TripStatus.CANCELLED)

    dispatch.release_trips(lead.reservations.all(), note="Order cancelled")

    assert not Task.objects.filter(lead=lead, status__in=Task.UNRESOLVED).exists()


def test_the_tick_catches_a_cancellation_no_hook_saw():
    lead = _order(trips=2)
    trip = lead.reservations.order_by("pickup_date").first()
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.CANCELLED)

    run_tasks()

    assert not Task.objects.filter(reservation=trip, status__in=Task.UNRESOLVED).exists()


def test_the_tick_catches_an_order_that_is_no_longer_booked():
    lead = _order()
    Lead.objects.filter(pk=lead.pk).update(status=Lead.Status.LOST)

    run_tasks()

    assert not Task.objects.filter(lead=lead, status__in=Task.UNRESOLVED).exists()


def test_two_consecutive_runs_change_nothing_the_second_time():
    lead = _order(trips=2)
    trip = lead.reservations.first()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    Assignment.objects.filter(pk=a.pk).update(affiliate_confirmed_at=timezone.now())
    before = Task.objects.count()

    assert run_tasks() > 0
    snapshot = list(Task.objects.values_list("pk", "status", "due_at", "updated_at"))

    assert run_tasks() == 0
    assert Task.objects.count() == before
    assert list(Task.objects.values_list("pk", "status", "due_at", "updated_at")) == snapshot


def test_query_count_is_bounded_for_fifty_orders():
    TaskConfig.load()
    for _ in range(5):
        _order()
    with CaptureQueriesContext(connection) as few:
        run_tasks()
    for _ in range(45):
        _order()
    with CaptureQueriesContext(connection) as many:
        run_tasks()

    assert len(many) == len(few)
    assert len(many) <= 20


def test_a_disabled_config_makes_the_job_a_no_op():
    lead = _order()
    trip = lead.reservations.get()
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.CANCELLED)
    cfg = TaskConfig.load()
    cfg.enabled = False
    cfg.save()

    assert run_tasks() == 0
    assert Task.objects.filter(reservation=trip, status=Task.Status.OPEN).exists()
