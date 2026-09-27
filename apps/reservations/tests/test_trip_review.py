"""APC-59 — the post-trip review: actual times, suggested overtime a person approves,
issues, and the affiliate's rating."""

from datetime import date, datetime, time, timedelta
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
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue, TripReview, TripStatusEvent
from apps.tasks import services as tasks
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig
from apps.tasks.selectors import attach_green_lit, green_lit

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _finished_trip(*, farmed_out=True, hours="3", zone="America/New_York", **extra):
    """A booked trip that ran yesterday 6-9 PM local and has entered post-trip."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(18, 0),
        pickup_timezone=zone,
        hours=Decimal(hours),
        **extra,
    )
    if farmed_out:
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    else:
        AssignmentFactory(reservation=trip, in_house=True)
    run_tasks()
    return trip


def _event(trip, status, at):
    event = TripStatusEvent.objects.create(reservation=trip, status=status)
    TripStatusEvent.objects.filter(pk=event.pk).update(created_at=at)


def _decided(trip, *, billable=0, waived=False, reason="", minutes_over=0):
    start = trip.pickup_at
    end = start + timedelta(hours=float(trip.billed_hours), minutes=minutes_over)
    return reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=end,
        billable_overtime_minutes=billable,
        overtime_waived=waived,
        waive_reason=reason,
    )


def _task(trip, kind):
    return Task.objects.get(reservation=trip, kind=kind)


# --- actual times --------------------------------------------------------------------


def test_actual_times_prefill_from_status_events():
    trip = _finished_trip()
    arrived = datetime(2026, 9, 25, 21, 50, tzinfo=ZoneInfo("UTC"))
    _event(trip, Reservation.TripStatus.ARRIVED, arrived)
    _event(trip, Reservation.TripStatus.CUSTOMER_IN_CAR, arrived + timedelta(minutes=12))
    _event(trip, Reservation.TripStatus.DONE, arrived + timedelta(hours=3, minutes=40))

    review = reviews.review_for(trip)

    assert review.actual_pickup_at == arrived
    assert review.actual_dropoff_at == arrived + timedelta(hours=3, minutes=40)


def test_customer_in_car_alone_is_the_pickup():
    trip = _finished_trip()
    in_car = datetime(2026, 9, 25, 22, 5, tzinfo=ZoneInfo("UTC"))
    _event(trip, Reservation.TripStatus.CUSTOMER_IN_CAR, in_car)

    review = reviews.review_for(trip)

    assert review.actual_pickup_at == in_car
    assert review.actual_dropoff_at is None


def test_with_no_events_the_times_are_entered_by_hand():
    trip = _finished_trip()
    review = reviews.review_for(trip)
    assert (review.actual_pickup_at, review.actual_dropoff_at) == (None, None)

    start = trip.pickup_at
    reviews.save_review(
        review,
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3),
    )

    review.refresh_from_db()
    assert review.actual_pickup_at == start
    assert review.actual_dropoff_at == start + timedelta(hours=3)


def test_review_for_is_one_row_per_trip():
    trip = _finished_trip()

    assert reviews.review_for(trip).pk == reviews.review_for(trip).pk
    assert TripReview.objects.filter(reservation=trip).count() == 1


def test_a_drop_off_before_the_pickup_is_refused():
    trip = _finished_trip()
    start = trip.pickup_at

    with pytest.raises(reviews.ReviewError):
        reviews.save_review(
            reviews.review_for(trip),
            user=UserFactory(),
            actual_pickup_at=start,
            actual_dropoff_at=start - timedelta(minutes=1),
        )


def test_times_round_trip_in_the_trip_zone_across_midnight():
    trip = _finished_trip(zone="America/Los_Angeles")
    pickup = reviews.from_local(trip, date(2026, 9, 25), time(22, 0))
    dropoff = reviews.from_local(trip, date(2026, 9, 26), time(1, 30))

    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=pickup,
        actual_dropoff_at=dropoff,
    )
    review.refresh_from_db()

    assert review.actual_dropoff_at == datetime(2026, 9, 26, 8, 30, tzinfo=ZoneInfo("UTC"))
    assert review.actual_pickup_local.date() == date(2026, 9, 25)
    assert review.actual_dropoff_local.date() == date(2026, 9, 26)
    assert review.actual_dropoff_display == "Sep 26, 1:30 AM PDT"
    assert review.actual_minutes == 210


# --- the overtime suggestion ---------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes_over", "increment", "grace", "expected"),
    [
        (-30, 15, 15, 0),  # finished early
        (0, 15, 15, 0),
        (10, 15, 15, 0),  # inside the grace
        (15, 15, 15, 0),  # exactly the grace still doesn't count
        (16, 15, 15, 30),  # past it: the whole overage, rounded up to the increment
        (30, 15, 15, 30),
        (31, 30, 0, 60),
        (40, 60, 15, 60),
        (61, 60, 15, 120),
        (7, 1, 0, 7),
    ],
)
def test_the_suggestion_honours_the_increment_and_grace(minutes_over, increment, grace, expected):
    start = datetime(2026, 9, 25, 22, 0, tzinfo=ZoneInfo("UTC"))
    end = start + timedelta(hours=3, minutes=minutes_over)

    got = reviews.suggest_overtime(
        Decimal("3"), start, end, increment_minutes=increment, grace_minutes=grace
    )

    assert got == expected


def test_saving_stores_the_suggestion_from_the_configured_settings():
    cfg = TaskConfig.load()
    cfg.overtime_increment_minutes = 30
    cfg.overtime_grace_minutes = 0
    cfg.save()
    trip = _finished_trip()

    review = _decided(trip, billable=0, minutes_over=20)

    assert review.suggested_overtime_minutes == 30


def test_the_placeholder_settings_default_to_fifteen_minutes():
    cfg = TaskConfig.load()

    assert (cfg.overtime_increment_minutes, cfg.overtime_grace_minutes) == (15, 15)


def test_billable_minutes_default_to_the_suggestion_until_someone_decides():
    trip = _finished_trip()
    start = trip.pickup_at

    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=40),
    )

    assert review.suggested_overtime_minutes == 45
    assert review.billable_overtime_minutes == 45
    assert review.decided_at is None


@pytest.mark.parametrize("billable", [0, 20, 90])
def test_billable_minutes_can_go_below_or_above_the_suggestion(billable):
    trip = _finished_trip()
    user = UserFactory()
    start = trip.pickup_at

    review = reviews.save_review(
        reviews.review_for(trip),
        user=user,
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=40),
        billable_overtime_minutes=billable,
    )

    assert review.suggested_overtime_minutes == 45
    assert review.billable_overtime_minutes == billable
    assert review.decided_by == user
    assert review.decided_at is not None


def test_waiving_needs_a_reason():
    trip = _finished_trip()

    with pytest.raises(reviews.ReviewError, match="reason"):
        _decided(trip, billable=45, waived=True, reason="  ", minutes_over=40)


def test_a_waived_review_bills_nothing():
    trip = _finished_trip()

    review = _decided(trip, billable=45, waived=True, reason="Traffic", minutes_over=40)

    assert review.overtime_waived
    assert review.waive_reason == "Traffic"
    assert review.billable_overtime_minutes == 0
    assert review.suggested_overtime_minutes == 45


# --- affiliate rating ----------------------------------------------------------------


def test_a_farmed_out_trip_takes_a_rating():
    trip = _finished_trip()
    review = reviews.review_for(trip)

    reviews.save_review(review, user=UserFactory(), affiliate_rating=4)

    review.refresh_from_db()
    assert review.affiliate_rating == 4


@pytest.mark.parametrize("rating", [0, 6])
def test_a_rating_must_be_one_to_five(rating):
    trip = _finished_trip()

    with pytest.raises(reviews.ReviewError):
        reviews.save_review(reviews.review_for(trip), user=UserFactory(), affiliate_rating=rating)


def test_an_in_house_trip_takes_no_rating():
    trip = _finished_trip(farmed_out=False)

    with pytest.raises(reviews.ReviewError, match="affiliate"):
        reviews.save_review(reviews.review_for(trip), user=UserFactory(), affiliate_rating=3)


# --- completing ----------------------------------------------------------------------


def test_completing_closes_the_ops_review_task():
    trip = _finished_trip()
    review = _decided(trip, billable=30, minutes_over=30)
    user = UserFactory()

    reviews.complete_review(review, user=user)

    review.refresh_from_db()
    assert review.completed_by == user and review.completed_at is not None
    ops = _task(trip, "ops_review")
    assert ops.status == Task.Status.DONE and ops.completed_by == user
    assert _task(trip, "overtime_invoiced").status == Task.Status.OPEN


def test_completing_needs_both_actual_times():
    trip = _finished_trip()
    review = reviews.review_for(trip)

    with pytest.raises(reviews.ReviewError, match="time"):
        reviews.complete_review(review, user=UserFactory())

    assert _task(trip, "ops_review").status == Task.Status.OPEN


def test_completing_needs_an_overtime_decision():
    trip = _finished_trip()
    start = trip.pickup_at
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=4),
    )

    with pytest.raises(reviews.ReviewError, match="overtime"):
        reviews.complete_review(review, user=UserFactory())


def test_completing_twice_is_refused():
    trip = _finished_trip()
    review = _decided(trip)
    reviews.complete_review(review, user=UserFactory())

    with pytest.raises(reviews.ReviewError):
        reviews.complete_review(review, user=UserFactory())


@pytest.mark.parametrize(
    "decision", [{"billable": 0}, {"billable": 30, "waived": True, "reason": "Our delay"}]
)
def test_nothing_billable_makes_overtime_invoicing_not_applicable(decision):
    trip = _finished_trip(farmed_out=False)
    review = _decided(trip, minutes_over=30, **decision)

    reviews.complete_review(review, user=UserFactory())

    assert _task(trip, "overtime_invoiced").status == Task.Status.NOT_APPLICABLE
    # In-house: nothing else in Stage 2, so Stage 3 opens straight away.
    assert _task(trip, "thank_you_sent").status == Task.Status.OPEN


def test_billable_overtime_leaves_overtime_invoicing_open():
    trip = _finished_trip(farmed_out=False)

    reviews.complete_review(_decided(trip, billable=15, minutes_over=20), user=UserFactory())

    assert _task(trip, "overtime_invoiced").status == Task.Status.OPEN
    assert not Task.objects.filter(reservation=trip, kind="thank_you_sent").exists()


def test_closing_the_ops_review_task_by_hand_doesnt_mark_overtime_not_applicable():
    """Only a completed review decides there's nothing to bill."""
    trip = _finished_trip(farmed_out=False)

    tasks.complete(_task(trip, "ops_review"), UserFactory())

    assert _task(trip, "overtime_invoiced").status == Task.Status.OPEN


# --- issues --------------------------------------------------------------------------


def test_logging_an_issue():
    trip = _finished_trip()
    user = UserFactory()

    issue = reviews.add_issue(
        reviews.review_for(trip),
        category=TripIssue.Category.VEHICLE,
        severity=TripIssue.Severity.HIGH,
        note="AC out the whole trip",
        user=user,
    )

    assert issue.review.reservation == trip
    assert issue.is_open
    assert issue.created_by == user


def test_an_issue_needs_a_note():
    trip = _finished_trip()

    with pytest.raises(reviews.ReviewError):
        reviews.add_issue(
            reviews.review_for(trip),
            category=TripIssue.Category.DRIVER,
            severity=TripIssue.Severity.LOW,
            note=" ",
            user=UserFactory(),
        )


def test_an_unknown_category_is_refused():
    trip = _finished_trip()

    with pytest.raises(reviews.ReviewError):
        reviews.add_issue(
            reviews.review_for(trip),
            category="weather",
            severity=TripIssue.Severity.LOW,
            note="Rain",
            user=UserFactory(),
        )


def test_an_open_issue_blocks_green_lit_until_resolved():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=5),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    tasks.ensure_tasks(lead)
    Task.objects.filter(lead=lead).update(status=Task.Status.DONE, completed_by=UserFactory())
    assert green_lit(trip)

    issue = reviews.add_issue(
        reviews.review_for(trip),
        category=TripIssue.Category.SERVICE,
        severity=TripIssue.Severity.MEDIUM,
        note="Customer asked for a car seat; affiliate hasn't confirmed one",
        user=UserFactory(),
    )

    assert not green_lit(trip)
    [annotated] = attach_green_lit(Reservation.objects.filter(pk=trip.pk).select_related("lead"))
    assert not annotated.green_lit
    assert "Open trip issue" in annotated.task_blockers

    reviews.resolve_issue(issue, user=UserFactory())

    assert green_lit(trip)
    [annotated] = attach_green_lit(Reservation.objects.filter(pk=trip.pk).select_related("lead"))
    assert annotated.green_lit


def test_open_issues_are_queryable_for_phase_d():
    trip = _finished_trip()
    review = reviews.review_for(trip)
    kwargs = {"severity": TripIssue.Severity.LOW, "note": "x", "user": UserFactory()}
    reviews.add_issue(review, category=TripIssue.Category.COMPLAINT, **kwargs)
    done = reviews.add_issue(review, category=TripIssue.Category.DRIVER, **kwargs)
    reviews.resolve_issue(done, user=UserFactory())

    assert list(TripIssue.objects.open().values_list("category", flat=True)) == ["complaint"]


# --- figures for the review screen: customer billing and driver pay (APC-61) ---------


def _coverage(trip):
    return Assignment.objects.active().filter(reservation=trip).first()


def test_figures_for_a_farmed_out_trip():
    trip = _finished_trip(hours="3", rate=Decimal("168"))
    Assignment.objects.filter(reservation=trip).update(payout=Decimal("420"))
    review = _decided(trip, billable=45, minutes_over=40)

    f = reviews.figures(review, _coverage(trip))

    assert (f.billed_minutes, f.actual_minutes, f.over_minutes) == (180, 220, 40)
    assert f.suggested_minutes == 45
    assert f.customer_rate == Decimal("168")
    assert f.customer_amount == Decimal("126.00")  # 45 min at $168/h
    assert f.coverage == "affiliate"
    assert f.payout == Decimal("420")
    # $420 isn't what the trip's factor pays, so the share is payout ÷ subtotal ($504).
    assert f.affiliate_share_basis == "payout"
    assert f.affiliate_overtime_amount == Decimal("105.00")  # $126 × 420/504
    assert f.expected_affiliate_amount == Decimal("525.00")


def test_a_waived_review_bills_nothing_and_pays_the_affiliate_no_overtime():
    """Affiliate overtime rises with what was billed (APC-61) — the override is how a
    waived trip still pays the affiliate for time they worked."""
    trip = _finished_trip(hours="3", rate=Decimal("168"))
    Assignment.objects.filter(reservation=trip).update(payout=Decimal("420"))
    review = _decided(trip, billable=45, waived=True, reason="Our delay", minutes_over=40)

    f = reviews.figures(review, _coverage(trip))

    assert f.customer_amount == Decimal("0.00")
    assert f.affiliate_overtime_amount == Decimal("0.00")
    assert f.expected_affiliate_amount == Decimal("420.00")


def test_figures_for_an_in_house_trip_have_no_payable():
    trip = _finished_trip(farmed_out=False)
    review = _decided(trip, minutes_over=20)

    f = reviews.figures(review, _coverage(trip))

    assert f.coverage == "in_house"
    assert f.expected_affiliate_amount is None
    assert f.driver_pay_amount is None  # no hourly rate on the driver yet


def test_figures_before_actual_times_are_entered():
    trip = _finished_trip()

    f = reviews.figures(reviews.review_for(trip), _coverage(trip))

    assert f.actual_minutes is None and f.over_minutes is None
    assert f.affiliate_overtime_amount == Decimal("0.00")
    assert "Enter the actual times" in f.rule
