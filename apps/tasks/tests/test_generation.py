"""APC-50 — the Task model, the kind registry, and generation at the seams."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import services as dispatch
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.fleet.factories import DriverFactory
from apps.leads import contact_roles
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

ORDER_KINDS = {"deposit_received", "contract_signed", "final_itinerary", "final_balance_paid"}
WEDDING_KINDS = {"wedding_names", "day_of_contact"}
TRIP_KINDS = {
    "details_finalized",
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
    DispatchAlertConfig.load()
    small = _order(trips=1)
    big = _order(trips=10)

    with CaptureQueriesContext(connection) as one:
        services.ensure_tasks(small)
    with CaptureQueriesContext(connection) as ten:
        services.ensure_tasks(big)

    assert len(ten) == len(one)
    assert (
        len(ten) <= 19
    )  # +1 farm-in fact (APC-92), +1 schedules, +1 dispatch config, +1 message rows, once per batch


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


# --- "Final balance paid" follows the payment terms (TASK-03) ------------------------
# The balance is charged `BALANCE_CHARGE_DAYS_BEFORE` days before the order's first pickup
# (`PaymentPlan.balance_due_date`), or by the next balance run when the order is booked
# inside that window. The task opens then and is due the Settings interval after it opens.

LA = ZoneInfo("America/Los_Angeles")
BOOKED_AT = datetime(2026, 11, 10, 19, 8, tzinfo=UTC)  # 11:08 AM PST, like lead 61


def _book_with_first_pickup(days_out: int):
    """Lead 61's shape: the first pickup in LA `days_out` days after booking, and a later
    DC trip. Booked (tasks generated) at BOOKED_AT."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    first = BOOKED_AT.astimezone(LA).date() + timedelta(days=days_out)
    ReservationFactory(
        lead=lead,
        pickup_date=first + timedelta(days=29),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    ReservationFactory(
        lead=lead, pickup_date=first, pickup_time=time(9, 0), pickup_timezone="America/Los_Angeles"
    )
    with patch("django.utils.timezone.now", return_value=BOOKED_AT):
        services.ensure_tasks(lead)
    return lead, first


def test_balance_task_opens_on_the_balance_date_at_midnight_in_the_first_pickups_zone():
    lead, first = _book_with_first_pickup(60)

    task = _task(lead, "final_balance_paid")
    balance_day = first - timedelta(days=settings.BALANCE_CHARGE_DAYS_BEFORE)
    assert balance_day == PaymentPlanFactory(lead=lead).balance_due_date
    assert task.status == Task.Status.SCHEDULED
    assert task.opens_at == datetime.combine(balance_day, time(0, 0), tzinfo=LA)
    assert task.due_at == task.opens_at + timedelta(days=2)
    assert task.due_at.astimezone(LA).time() == time(0, 0)


@pytest.mark.parametrize("days_out", [20, 2])
def test_booked_inside_the_balance_window_the_task_opens_at_booking_and_is_not_overdue(days_out):
    """The balance date has passed, so the next balance run charges it: the task opens at
    booking and gets the Settings interval from there, never a due date in the past."""
    lead, _first = _book_with_first_pickup(days_out)

    task = _task(lead, "final_balance_paid")
    assert task.status == Task.Status.OPEN
    assert task.opens_at == BOOKED_AT
    assert task.due_at == BOOKED_AT + timedelta(days=2)
    with patch("django.utils.timezone.now", return_value=BOOKED_AT + timedelta(minutes=1)):
        assert not task.is_overdue_now


def test_no_balance_task_is_born_overdue_whatever_the_first_pickup():
    for days_out in (60, 31, 30, 29, 20, 2, 0):
        lead, _first = _book_with_first_pickup(days_out)
        task = _task(lead, "final_balance_paid")
        assert task.due_at > BOOKED_AT, days_out
        assert task.due_at > task.opens_at, days_out


def test_moving_the_first_pickup_into_the_window_opens_the_task_with_a_fresh_due():
    lead, first = _book_with_first_pickup(60)
    trip = lead.reservations.get(pickup_timezone="America/Los_Angeles")
    trip.pickup_date = first - timedelta(days=45)  # now 15 days out
    trip.save(update_fields=["pickup_date"])
    later = BOOKED_AT + timedelta(days=1)

    with patch("django.utils.timezone.now", return_value=later):
        services.reschedule(lead)

    task = _task(lead, "final_balance_paid")
    assert task.status == Task.Status.OPEN
    assert task.opens_at == later
    assert task.due_at == later + timedelta(days=2)


def test_moving_the_first_pickup_out_re_dates_a_scheduled_balance_task():
    lead, first = _book_with_first_pickup(60)
    trip = lead.reservations.get(pickup_timezone="America/Los_Angeles")
    trip.pickup_date = first + timedelta(days=7)
    trip.save(update_fields=["pickup_date"])

    with patch("django.utils.timezone.now", return_value=BOOKED_AT):
        services.reschedule(lead)

    task = _task(lead, "final_balance_paid")
    balance_day = first + timedelta(days=7 - settings.BALANCE_CHARGE_DAYS_BEFORE)
    assert task.status == Task.Status.SCHEDULED
    assert task.opens_at == datetime.combine(balance_day, time(0, 0), tzinfo=LA)
    assert task.due_at == task.opens_at + timedelta(days=2)


def test_the_checklist_schedule_sets_the_interval_after_the_balance_task_opens():
    from apps.tasks import schedule

    lead, _first = _book_with_first_pickup(20)

    with patch("django.utils.timezone.now", return_value=BOOKED_AT):
        schedule.save({("final_balance_paid", "standard"): (5, "days")}, user=UserFactory())

    task = _task(lead, "final_balance_paid")
    assert task.opens_at == BOOKED_AT
    assert task.due_at == BOOKED_AT + timedelta(days=5)
    rows = {(r.key, r.variant): r for r in schedule.ROWS}
    assert rows[("final_balance_paid", "standard")].anchor == "after it opens"


@pytest.mark.parametrize(
    ("amount", "unit", "expected"),
    [
        (28, "days", None),  # the old default: 2 days after the T-30 opening
        (25, "days", (5, "days")),
        (35, "days", (0, "days")),  # was due before it opened: due as it opens
        (700, "hours", (20, "hours")),
        (48, "hours", (672, "hours")),
    ],
)
def test_migration_turns_a_saved_before_pickup_row_into_an_interval_after_opening(
    amount, unit, expected
):
    import importlib

    migration = importlib.import_module("apps.tasks.migrations.0019_balance_due_after_open")

    assert migration.interval_for(amount, unit) == expected


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

    Lead.objects.filter(pk=lead.pk).update(wedding_name="Smith / Jones")
    # APC-64: the day-of contact is the day-of coordinator role, not the old columns.
    contact_roles.set_day_of_contact(lead, name="Ana", phone="")
    services.evaluate_lead(lead)
    assert _task(lead, "wedding_names").status == Task.Status.DONE
    assert _task(lead, "day_of_contact").status == Task.Status.OPEN  # no phone yet

    contact_roles.set_day_of_contact(lead, name="Ana", phone="+15715551212")
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

    with patch.object(
        payments.stripe.PaymentIntent,
        "create",
        return_value=MagicMock(id="pi_1", status="succeeded"),
    ):
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
