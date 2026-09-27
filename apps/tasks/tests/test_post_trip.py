"""APC-58 — post-trip entry and stage chaining (ops review → accounting → customer service)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status
from apps.tasks import services
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig
from apps.tasks.selectors import green_lit

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")
STAGE_2 = {"overtime_invoiced", "affiliate_payable_approved"}
POST_TRIP = {"ops_review", "overtime_invoiced", "affiliate_payable_approved", "affiliate_paid"}
POST_TRIP |= {"thank_you_sent"}


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _trip(*, started_ago: timedelta, billed_hours="1", farmed_out=True, **extra):
    """A booked one-trip order whose pickup was `started_ago` (negative = still ahead)."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    start = (timezone.now() - started_ago).astimezone(NY).replace(second=0, microsecond=0)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=start.date(),
        pickup_time=start.time(),
        pickup_timezone="America/New_York",
        hours=Decimal(billed_hours),
        **extra,
    )
    if farmed_out:
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    else:
        AssignmentFactory(reservation=trip, in_house=True)
    return trip


def _kinds(trip) -> set[str]:
    return set(Task.objects.filter(reservation=trip).values_list("kind", flat=True))


def _post_trip(trip) -> dict[str, str]:
    rows = Task.objects.filter(reservation=trip, kind__in=POST_TRIP)
    return dict(rows.values_list("kind", "status"))


def _close(trip, kind, user=None):
    services.complete(Task.objects.get(reservation=trip, kind=kind), user or UserFactory())


# --- entry ---------------------------------------------------------------------------


def test_a_done_status_opens_the_ops_review_once():
    trip = _trip(started_ago=-timedelta(hours=1))  # pickup still an hour away
    services.ensure_tasks(trip.lead)
    assert "ops_review" not in _kinds(trip)

    set_trip_status(trip, Reservation.TripStatus.DONE)
    set_trip_status(trip, Reservation.TripStatus.CUSTOMER_IN_CAR)
    set_trip_status(trip, Reservation.TripStatus.DONE)
    run_tasks()

    assert _post_trip(trip) == {"ops_review": Task.Status.OPEN}
    review = Task.objects.get(reservation=trip, kind="ops_review")
    assert review.department == "operations"
    assert review.due_at is not None


def test_no_status_enters_once_the_scheduled_end_is_past_the_grace():
    # 1h billed, picked up 3h05m ago → ended 2h05m ago, past the 2h default grace.
    trip = _trip(started_ago=timedelta(hours=3, minutes=5))

    run_tasks()

    assert _post_trip(trip) == {"ops_review": Task.Status.OPEN}


def test_nothing_opens_inside_the_grace_period():
    # Ended 1h ago; the grace runs another hour.
    trip = _trip(started_ago=timedelta(hours=2))

    run_tasks()

    assert "ops_review" not in _kinds(trip)


def test_the_grace_is_a_setting():
    cfg = TaskConfig.load()
    cfg.post_trip_grace_hours = 0
    cfg.save()
    trip = _trip(started_ago=timedelta(hours=2))

    run_tasks()

    assert "ops_review" in _kinds(trip)


def test_a_drop_off_time_wins_over_pickup_plus_billed_hours():
    # Pickup + 1h ended 4h ago, but the drop-off is 30 minutes ago: still inside the grace.
    trip = _trip(started_ago=timedelta(hours=5))
    end = (timezone.now() - timedelta(minutes=30)).astimezone(NY)
    Reservation.objects.filter(pk=trip.pk).update(dropoff_date=end.date(), dropoff_time=end.time())

    run_tasks()

    assert "ops_review" not in _kinds(trip)


@pytest.mark.parametrize(
    "status", [Reservation.TripStatus.CANCELLED, Reservation.TripStatus.NO_SHOW]
)
def test_a_cancelled_trip_never_enters(status):
    trip = _trip(started_ago=timedelta(hours=6))
    Reservation.objects.filter(pk=trip.pk).update(trip_status=status)

    run_tasks()

    assert "ops_review" not in _kinds(trip)


def test_an_unbooked_order_never_enters():
    trip = _trip(started_ago=timedelta(hours=6))
    Lead.objects.filter(pk=trip.lead_id).update(status=Lead.Status.LOST)

    run_tasks()

    assert not Task.objects.filter(reservation=trip).exists()


def test_the_cron_does_not_backfill_reviews_for_long_finished_trips():
    trip = _trip(started_ago=timedelta(days=30))

    run_tasks()

    assert "ops_review" not in _kinds(trip)


def test_a_finished_trip_gets_no_pre_trip_tasks():
    """A trip already over has nothing left to dispatch — generating "affiliate assigned"
    for it would only raise an overdue alert on the next tick."""
    trip = _trip(started_ago=timedelta(hours=6))

    services.ensure_tasks(trip.lead)

    assert _kinds(trip) == {"ops_review"}


# --- chaining ------------------------------------------------------------------------


def test_stage_two_waits_for_the_ops_review():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()
    assert _post_trip(trip) == {"ops_review": Task.Status.OPEN}

    _close(trip, "ops_review")

    assert _post_trip(trip) == {
        "ops_review": Task.Status.DONE,
        "overtime_invoiced": Task.Status.OPEN,
        "affiliate_payable_approved": Task.Status.OPEN,
    }


def test_affiliate_paid_waits_for_the_approval():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()
    _close(trip, "ops_review")

    _close(trip, "affiliate_payable_approved")

    assert _post_trip(trip)["affiliate_paid"] == Task.Status.OPEN
    assert "thank_you_sent" not in _post_trip(trip)


def test_stage_three_waits_for_all_of_stage_two():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()
    _close(trip, "ops_review")
    _close(trip, "affiliate_payable_approved")
    _close(trip, "affiliate_paid")
    assert "thank_you_sent" not in _post_trip(trip)

    _close(trip, "overtime_invoiced")

    assert _post_trip(trip)["thank_you_sent"] == Task.Status.OPEN
    thanks = Task.objects.get(reservation=trip, kind="thank_you_sent")
    assert thanks.department == "customer_service"


def test_skipping_a_stage_counts_as_closing_it():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()

    services.skip(Task.objects.get(reservation=trip, kind="ops_review"), UserFactory(), "n/a")

    assert STAGE_2 <= set(_post_trip(trip))


def test_an_in_house_trip_gets_no_payable_kinds():
    trip = _trip(started_ago=timedelta(hours=6), farmed_out=False)
    run_tasks()
    _close(trip, "ops_review")

    assert _post_trip(trip) == {
        "ops_review": Task.Status.DONE,
        "overtime_invoiced": Task.Status.OPEN,
    }

    _close(trip, "overtime_invoiced")

    assert _post_trip(trip)["thank_you_sent"] == Task.Status.OPEN


def test_a_trip_with_no_coverage_gets_no_payable_kinds():
    trip = _trip(started_ago=timedelta(hours=6))
    Assignment.objects.filter(reservation=trip).update(status=Assignment.Status.WITHDRAWN)
    run_tasks()
    _close(trip, "ops_review")

    assert set(_post_trip(trip)) == {"ops_review", "overtime_invoiced"}


def test_stages_advance_on_the_tick_when_a_hook_was_missed():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()
    # Closed by a path with no hook (e.g. the admin).
    Task.objects.filter(reservation=trip, kind="ops_review").update(
        status=Task.Status.DONE, completed_at=timezone.now()
    )

    run_tasks()

    assert STAGE_2 <= set(_post_trip(trip))


def test_ticks_are_idempotent():
    trip = _trip(started_ago=timedelta(hours=6))
    run_tasks()
    _close(trip, "ops_review")
    run_tasks()
    snapshot = sorted(Task.objects.values_list("pk", "kind", "status", "updated_at"))

    assert run_tasks() == 0
    assert sorted(Task.objects.values_list("pk", "kind", "status", "updated_at")) == snapshot


def test_post_trip_work_does_not_take_the_green_lit_away():
    """Green-lit is a pre-trip dispatch state; the review that follows the trip isn't
    something dispatch is waiting on."""
    trip = _trip(started_ago=-timedelta(hours=1))
    services.ensure_tasks(trip.lead)
    Task.objects.filter(reservation=trip).update(
        status=Task.Status.DONE, completed_by=UserFactory()
    )
    assert green_lit(trip)

    set_trip_status(trip, Reservation.TripStatus.DONE)

    assert _post_trip(trip) == {"ops_review": Task.Status.OPEN}
    assert green_lit(trip)
