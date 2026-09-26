"""APC-50 — the Task model, the kind registry, and generation at the seams."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import services as dispatch
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.leads.services import book_lead
from apps.messaging.models import TouchPoint
from apps.payments import services as payments
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import ReservationFactory
from apps.tasks import services
from apps.tasks.definitions import KINDS
from apps.tasks.models import Task, TaskConfig

pytestmark = pytest.mark.django_db

ORDER_KINDS = {"deposit_received", "final_itinerary", "final_balance_paid"}
WEDDING_KINDS = {"wedding_names", "day_of_contact"}
TRIP_KINDS = {
    "affiliate_assigned",
    "affiliate_confirmed",
    "driver_assigned",
    "driver_info_received",
    "driver_released",
}


@pytest.fixture(autouse=True)
def _no_la_push():
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _future(days=20):
    return timezone.localdate() + timedelta(days=days)


def _order(trips=1, *, wedding=False, status=Lead.Status.BOOKED, **lead_kwargs):
    lead = LeadFactory(status=status, **lead_kwargs)
    service = ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None
    for i in range(trips):
        ReservationFactory(
            lead=lead,
            service_type=service,
            pickup_date=_future(20 + i),
            pickup_time=time(9, 0),
            pickup_timezone="America/New_York",
        )
    return lead


def _task(lead, kind, reservation=None):
    return Task.objects.get(lead=lead, kind=kind, reservation=reservation)


def _kinds(lead):
    return sorted(Task.objects.filter(lead=lead).values_list("kind", flat=True))


# --- generation ---------------------------------------------------------------------


def test_booking_creates_the_order_and_trip_kinds():
    lead = _order(trips=2, status=Lead.Status.QUOTED)

    book_lead(lead)

    assert _kinds(lead) == sorted([*ORDER_KINDS, *TRIP_KINDS, *TRIP_KINDS])
    assert Task.objects.filter(lead=lead, reservation__isnull=True).count() == len(ORDER_KINDS)


def test_a_wedding_order_also_gets_the_wedding_kinds():
    lead = _order(wedding=True, status=Lead.Status.QUOTED)

    book_lead(lead)

    assert set(_kinds(lead)) == ORDER_KINDS | WEDDING_KINDS | TRIP_KINDS


def test_running_generation_twice_creates_nothing_new():
    lead = _order(trips=3, status=Lead.Status.QUOTED)
    book_lead(lead)
    before = Task.objects.count()

    services.ensure_tasks(lead)
    book_lead(lead)

    assert Task.objects.count() == before


def test_an_unbooked_order_gets_no_tasks():
    lead = _order(status=Lead.Status.QUOTED)

    services.ensure_tasks(lead)

    assert not Task.objects.filter(lead=lead).exists()


def test_a_disabled_config_generates_nothing():
    cfg = TaskConfig.load()
    cfg.enabled = False
    cfg.save()
    lead = _order(status=Lead.Status.QUOTED)

    book_lead(lead)

    assert not Task.objects.filter(lead=lead).exists()


def test_a_trip_added_after_booking_gets_its_tasks_on_the_next_ensure():
    lead = _order()
    services.ensure_tasks(lead)
    extra = ReservationFactory(lead=lead, pickup_date=_future(40), pickup_time=time(9, 0))

    services.ensure_tasks(lead)

    assert Task.objects.filter(reservation=extra).count() == len(TRIP_KINDS)


def test_every_task_carries_its_kind_department():
    lead = _order()
    services.ensure_tasks(lead)

    for task in Task.objects.filter(lead=lead):
        assert task.department == KINDS[task.kind].department


def test_default_assignee_comes_from_the_department_owner():
    owner = UserFactory()
    cfg = TaskConfig.load()
    cfg.accounting_owner = owner
    cfg.save()
    lead = _order()

    services.ensure_tasks(lead)

    assert _task(lead, "final_balance_paid").assignee == owner
    assert _task(lead, "deposit_received").assignee is None


def test_ensure_tasks_query_count_does_not_grow_with_trips():
    TaskConfig.load()
    small = _order(trips=1)
    big = _order(trips=10)

    with CaptureQueriesContext(connection) as one:
        services.ensure_tasks(small)
    with CaptureQueriesContext(connection) as ten:
        services.ensure_tasks(big)

    assert len(ten) == len(one)
    assert len(ten) <= 15


# --- due dates ----------------------------------------------------------------------


def test_a_day_boundary_is_computed_in_the_trip_timezone():
    """11:30 PM Eastern on Oct 10 is already Oct 11 in UTC. "Start of the day, a week out"
    must mean Oct 3 00:00 Eastern (04:00 UTC), not Oct 4 00:00 UTC."""
    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="")
    ReservationFactory(
        lead=lead,
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME),
        pickup_date=date(2026, 10, 10),
        pickup_time=time(23, 30),
        pickup_timezone="America/New_York",
    )
    assert KINDS["wedding_names"].due.day_start is True
    days = KINDS["wedding_names"].due.days

    with patch("django.utils.timezone.now", return_value=datetime(2026, 9, 1, tzinfo=UTC)):
        services.ensure_tasks(lead)

    local_day = date(2026, 10, 10) - timedelta(days=days)
    expected = datetime(local_day.year, local_day.month, local_day.day, 4, 0, tzinfo=UTC)
    assert _task(lead, "wedding_names").due_at == expected


def test_trip_level_due_dates_follow_that_trips_pickup():
    lead = _order(trips=2)
    services.ensure_tasks(lead)

    first, second = lead.reservations.order_by("pickup_date")
    a = _task(lead, "affiliate_assigned", first).due_at
    b = _task(lead, "affiliate_assigned", second).due_at

    assert b - a == timedelta(days=1)


def test_a_due_date_already_past_at_booking_is_clamped_to_now():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, pickup_date=timezone.localdate(), pickup_time=time(23, 0))

    services.ensure_tasks(lead)

    task = _task(lead, "final_balance_paid")
    assert task.due_at >= task.opens_at


def test_a_future_opening_task_is_scheduled_not_open():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead, pickup_date=_future(60), pickup_time=time(9, 0))
    services.ensure_tasks(lead)

    task = _task(lead, "final_balance_paid")
    assert KINDS["final_balance_paid"].opens is not None
    assert task.opens_at > timezone.now()
    assert task.status == Task.Status.SCHEDULED


# --- auto-complete predicates ------------------------------------------------------


def test_deposit_paid_closes_deposit_received():
    lead = _order()
    PaymentPlanFactory(lead=lead, deposit_status=PaymentPlan.DepositStatus.PAID)

    services.ensure_tasks(lead)

    task = _task(lead, "deposit_received")
    assert task.status == Task.Status.DONE
    assert task.completed_by is None
    assert task.completed_at is not None


def test_wedding_answers_close_the_wedding_tasks():
    lead = _order(wedding=True)
    services.ensure_tasks(lead)
    assert _task(lead, "wedding_names").status == Task.Status.OPEN

    Lead.objects.filter(pk=lead.pk).update(wedding_name="Smith / Jones", day_of_contact_name="Ana")
    services.evaluate_lead(lead)
    assert _task(lead, "wedding_names").status == Task.Status.DONE
    assert _task(lead, "day_of_contact").status == Task.Status.OPEN  # no phone yet

    Lead.objects.filter(pk=lead.pk).update(day_of_contact_phone="+15715551212")
    services.evaluate_lead(lead)
    assert _task(lead, "day_of_contact").status == Task.Status.DONE


def test_final_itinerary_is_manual_only():
    lead = _order()
    services.ensure_tasks(lead)

    services.evaluate_lead(lead)

    assert _task(lead, "final_itinerary").status == Task.Status.OPEN


def test_an_offer_closes_affiliate_assigned_and_confirmation_closes_confirmed():
    lead = _order()
    trip = lead.reservations.get()
    services.ensure_tasks(lead)

    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    services.evaluate_lead(lead)
    assert _task(lead, "affiliate_assigned", trip).status == Task.Status.DONE
    assert _task(lead, "affiliate_confirmed", trip).status == Task.Status.OPEN

    offer.affiliate_confirmed_at = timezone.now()
    offer.status = Assignment.Status.CONFIRMED
    offer.save()
    services.evaluate_lead(lead)
    assert _task(lead, "affiliate_confirmed", trip).status == Task.Status.DONE


def test_driver_info_through_set_driver_info_closes_the_driver_tasks():
    lead = _order()
    trip = lead.reservations.get()
    services.ensure_tasks(lead)
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    dispatch.set_driver_info(
        a, name="Sam Rivera", cell="+15715551212", vehicle_desc="Suburban", vehicle_number="7"
    )

    assert _task(lead, "driver_assigned", trip).status == Task.Status.DONE
    assert _task(lead, "driver_info_received", trip).status == Task.Status.DONE


def test_in_house_coverage_closes_the_affiliate_and_driver_tasks():
    lead = _order()
    trip = lead.reservations.get()
    services.ensure_tasks(lead)

    dispatch.assign_in_house(trip, DriverFactory(phone="+15715551212"))

    for kind in ("affiliate_assigned", "affiliate_confirmed", "driver_assigned"):
        assert _task(lead, kind, trip).status == Task.Status.DONE, kind
    assert _task(lead, "driver_info_received", trip).status == Task.Status.DONE


def test_dispatch_confirm_is_a_generation_seam():
    lead = _order()
    trip = lead.reservations.get()
    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)

    dispatch.confirm(offer)

    assert _task(lead, "affiliate_assigned", trip).status == Task.Status.DONE


def test_a_sent_driver_released_touchpoint_closes_driver_released():
    lead = _order()
    trip = lead.reservations.get()
    services.ensure_tasks(lead)
    TouchPoint.objects.create(
        lead=lead,
        reservation=trip,
        kind=TouchPoint.Kind.DRIVER_RELEASED,
        status=TouchPoint.Status.SCHEDULED,
    )
    services.evaluate_lead(lead)
    assert _task(lead, "driver_released", trip).status == Task.Status.OPEN

    TouchPoint.objects.filter(reservation=trip).update(status=TouchPoint.Status.SENT)
    services.evaluate_lead(lead)

    assert _task(lead, "driver_released", trip).status == Task.Status.DONE


def test_the_balance_cron_path_closes_final_balance_paid():
    lead = _order()
    services.ensure_tasks(lead)
    plan = PaymentPlanFactory(
        lead=lead,
        quote_total=Decimal("1000.00"),
        deposit_status=PaymentPlan.DepositStatus.PAID,
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
    )

    with patch.object(payments.stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_1")):
        payments.charge_balance(plan)

    assert _task(lead, "final_balance_paid").status == Task.Status.DONE


def test_record_payment_closes_deposit_received():
    lead = _order(status=Lead.Status.QUOTED)
    plan = PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"))
    charge = plan.record_charge(kind="deposit", amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_dep"
    charge.save()
    intent = MagicMock(id="pi_dep", status="succeeded", payment_method=None)

    with patch.object(payments.stripe.PaymentIntent, "retrieve", return_value=intent):
        payments.record_payment(plan, "pi_dep", kind="deposit")

    assert _task(lead, "deposit_received").status == Task.Status.DONE


# --- manual override ---------------------------------------------------------------


def test_a_person_closing_a_task_early_keeps_it_closed():
    user = UserFactory()
    lead = _order()
    trip = lead.reservations.get()
    services.ensure_tasks(lead)
    task = _task(lead, "affiliate_assigned", trip)

    services.complete(task, user=user, note="Arranged by phone")
    services.evaluate_lead(lead)

    task.refresh_from_db()
    assert task.status == Task.Status.DONE
    assert task.completed_by == user
    assert task.note == "Arranged by phone"


def test_a_system_closed_task_reopens_when_its_data_goes_away():
    lead = _order()
    trip = lead.reservations.get()
    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    services.ensure_tasks(lead)
    assert _task(lead, "affiliate_assigned", trip).status == Task.Status.DONE

    dispatch.withdraw(offer)

    assert _task(lead, "affiliate_assigned", trip).status == Task.Status.OPEN


def test_a_person_closed_task_does_not_reopen_when_data_goes_away():
    user = UserFactory()
    lead = _order()
    trip = lead.reservations.get()
    offer = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    services.ensure_tasks(lead)
    task = _task(lead, "affiliate_assigned", trip)
    services.reopen(task, user=user)
    services.complete(task, user=user)

    dispatch.withdraw(offer)

    assert _task(lead, "affiliate_assigned", trip).status == Task.Status.DONE


def test_skip_needs_a_note_and_records_who():
    user = UserFactory()
    lead = _order()
    services.ensure_tasks(lead)
    task = _task(lead, "final_itinerary")

    with pytest.raises(services.TaskError):
        services.skip(task, user=user, note="  ")

    services.skip(task, user=user, note="Single transfer, no itinerary")
    task.refresh_from_db()
    assert task.status == Task.Status.SKIPPED
    assert task.completed_by == user


def test_reopen_clears_the_completion():
    user = UserFactory()
    lead = _order()
    services.ensure_tasks(lead)
    task = _task(lead, "final_itinerary")
    services.complete(task, user=user)

    services.reopen(task, user=user)

    task.refresh_from_db()
    assert task.status == Task.Status.OPEN
    assert task.completed_at is None
    assert task.completed_by is None
