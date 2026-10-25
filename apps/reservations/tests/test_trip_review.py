"""APC-59 — the post-trip review: actual times, suggested overtime a person approves,
issues, and the affiliate's rating."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirportFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead, VehicleType
from apps.reservations import reviews
from apps.reservations.factories import FlightFactory, ReservationFactory
from apps.reservations.models import (
    FlightDirection,
    Reservation,
    TripIssue,
    TripReview,
    TripStatusEvent,
)
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
    """A booked hourly trip that ran yesterday 6-9 PM local and has entered post-trip.
    Pass `trip_type=Reservation.TripType.TRANSFER` for a transfer."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    extra.setdefault("trip_type", Reservation.TripType.HOURLY)
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
    "over, grace, expected",
    [
        (5, 5, 0),  # inside the grace: nothing
        (6, 5, 60),  # one minute past it: a full hour
        (70, 5, 120),  # 1 h 10 over → two hours started
        (15, 15, 0),
        (16, 15, 60),
        (None, 5, 0),  # no times yet
        (0, 0, 0),
        (1, 0, 60),
    ],
)
def test_the_suggestion_rounds_minutes_past_the_grace_up_to_whole_hours(over, grace, expected):
    assert reviews.suggest_overtime(over, grace_minutes=grace, block_minutes=60) == expected


def _bus():
    return VehicleTypeFactory(name="Coach", overtime_class=VehicleType.OvertimeClass.LARGE)


def _sedan():
    return VehicleTypeFactory(name="Sedan", overtime_class=VehicleType.OvertimeClass.STANDARD)


def test_a_large_vehicle_gets_the_five_minute_grace_on_hourly():
    trip = _finished_trip(vehicle=_bus())

    assert _decided(trip, minutes_over=5).suggested_overtime_minutes == 0
    assert _decided(trip, minutes_over=6).suggested_overtime_minutes == 60


def test_a_standard_vehicle_gets_the_fifteen_minute_grace_on_hourly():
    trip = _finished_trip(vehicle=_sedan())

    assert _decided(trip, minutes_over=15).suggested_overtime_minutes == 0
    assert _decided(trip, minutes_over=16).suggested_overtime_minutes == 60


def test_a_trip_with_no_vehicle_uses_the_standard_grace():
    trip = _finished_trip(vehicle=None)

    assert _decided(trip, minutes_over=15).suggested_overtime_minutes == 0
    assert _decided(trip, minutes_over=16).suggested_overtime_minutes == 60


def test_saving_stores_the_suggestion_from_the_configured_settings():
    cfg = TaskConfig.load()
    cfg.grace_standard_minutes = 0
    cfg.overtime_block_minutes = 30
    cfg.save()
    trip = _finished_trip(vehicle=_sedan())

    review = _decided(trip, billable=0, minutes_over=20)

    assert review.suggested_overtime_minutes == 30


# --- transfers: the clock is the wait at pickup --------------------------------------


def _transfer(**extra):
    extra.setdefault("vehicle", _bus())
    return _finished_trip(trip_type=Reservation.TripType.TRANSFER, hours="1", **extra)


def _waited(trip, minutes, *, ride_hours=3):
    """The driver picked up `minutes` after the scheduled time and the ride ran long."""
    pickup = trip.pickup_at + timedelta(minutes=minutes)
    return reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=pickup,
        actual_dropoff_at=pickup + timedelta(hours=ride_hours),
    )


def test_a_transfer_bills_the_wait_at_pickup_not_the_duration():
    trip = _transfer()

    review = _waited(trip, 20, ride_hours=3)  # a 3-hour ride on a 1-hour transfer

    assert review.suggested_overtime_minutes == 60  # 20 - 15 grace → one hour started
    assert reviews.basis_for(trip)[0] == reviews.OvertimeBasis.TRANSFER_WAIT


def test_a_transfer_inside_the_transfer_grace_suggests_nothing():
    assert _waited(_transfer(), 14).suggested_overtime_minutes == 0  # even on a bus


def test_an_early_pickup_on_a_transfer_suggests_nothing():
    trip = _transfer()

    review = _waited(trip, -10)
    f = reviews.figures(review, None)

    assert review.suggested_overtime_minutes == 0
    assert f.over_minutes == 0


# --- airport arrivals: the clock is the flight's actual arrival --------------------------


def _arrival(trip, *, origin_country="US", **flight_fields):
    """Make the trip's pickup an arriving flight at an airport; `flight_fields` set the
    flight's times (actual_at / estimated_at / scheduled_at)."""
    airport = AirportFactory()
    origin = AirportFactory(country=origin_country)
    flight_fields.setdefault("scheduled_at", None)
    flight = FlightFactory(
        airport=airport,
        direction=FlightDirection.ARRIVAL,
        flight_date=trip.pickup_date,
        other_airport_iata=origin.iata,
        **flight_fields,
    )
    stop = trip.stops.order_by("sequence").first()
    stop.airport = airport
    stop.airline = flight.airline
    stop.flight_number = flight.flight_number
    stop.flight_direction = FlightDirection.ARRIVAL
    stop.flight = flight
    stop.save()
    return flight


def _picked_up(trip, at):
    return reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=at,
        actual_dropoff_at=at + timedelta(hours=1),
    )


def test_a_domestic_arrival_gets_twenty_five_minutes():
    trip = _transfer()
    landed = trip.pickup_at - timedelta(minutes=5)
    _arrival(trip, actual_at=landed)

    assert _picked_up(trip, landed + timedelta(minutes=25)).suggested_overtime_minutes == 0
    assert _picked_up(trip, landed + timedelta(minutes=30)).suggested_overtime_minutes == 60
    assert reviews.basis_for(trip)[0] == reviews.OvertimeBasis.AIRPORT_ARRIVAL


def test_an_international_arrival_gets_fifty_minutes():
    trip = _transfer()
    landed = trip.pickup_at
    _arrival(trip, origin_country="GB", actual_at=landed)

    assert _picked_up(trip, landed + timedelta(minutes=50)).suggested_overtime_minutes == 0
    assert _picked_up(trip, landed + timedelta(minutes=51)).suggested_overtime_minutes == 60


def test_a_puerto_rico_origin_is_domestic():
    trip = _transfer()
    landed = trip.pickup_at
    _arrival(trip, origin_country="PR", actual_at=landed)

    assert _picked_up(trip, landed + timedelta(minutes=30)).suggested_overtime_minutes == 60


def test_the_arrival_clock_falls_back_to_estimated_then_scheduled():
    trip = _transfer()
    scheduled = trip.pickup_at - timedelta(minutes=40)
    estimated = trip.pickup_at - timedelta(minutes=10)
    _arrival(trip, scheduled_at=scheduled, estimated_at=estimated)

    # 30 min after the estimate (70 after the schedule): the estimate wins → one hour
    review = _picked_up(trip, estimated + timedelta(minutes=30))
    assert review.suggested_overtime_minutes == 60
    assert reviews.figures(review, None).over_minutes == 30

    flight = trip.stops.order_by("sequence").first().flight
    flight.estimated_at = None
    flight.save()
    review = _picked_up(trip, scheduled + timedelta(minutes=20))
    assert review.suggested_overtime_minutes == 0  # 20 after the schedule, inside 25


def test_an_airport_pickup_with_no_verified_flight_uses_the_wait_rule_and_says_so():
    trip = _transfer()
    stop = trip.stops.order_by("sequence").first()
    stop.airport = AirportFactory()
    stop.flight_number = "123"
    stop.save()

    review = _waited(trip, 20)
    f = reviews.figures(review, None)

    assert reviews.basis_for(trip)[0] == reviews.OvertimeBasis.TRANSFER_WAIT
    assert review.suggested_overtime_minutes == 60
    assert f.basis == "transfer_wait"
    assert f.rule.endswith(
        "No verified arrival time for the flight, so the transfer wait rule applies."
    )


def test_on_a_transfer_the_prefilled_pickup_is_the_boarding_not_the_drivers_arrival():
    """The wait clock runs until the passenger boards; a driver who arrives early must not
    zero the suggestion."""
    trip = _transfer()
    scheduled = trip.pickup_at
    _event(trip, Reservation.TripStatus.ARRIVED, scheduled - timedelta(minutes=5))
    _event(trip, Reservation.TripStatus.CUSTOMER_IN_CAR, scheduled + timedelta(minutes=45))
    _event(trip, Reservation.TripStatus.DONE, scheduled + timedelta(hours=2))

    review = reviews.review_for(trip)

    assert review.actual_pickup_at == scheduled + timedelta(minutes=45)
    assert review.suggested_overtime_minutes == 60


def test_the_arrival_copy_names_which_flight_time_was_used():
    trip = _transfer()
    scheduled = trip.pickup_at
    _arrival(trip, scheduled_at=scheduled)  # verified in advance, never refreshed day-of

    f = reviews.figures(_picked_up(trip, scheduled + timedelta(minutes=40)), None)

    assert f.rule == (
        "40 min after the flight's scheduled arrival; 25-min domestic grace, so 1 hour bills."
    )


def test_an_arrival_with_no_times_at_all_uses_the_wait_rule():
    trip = _transfer()
    _arrival(trip)  # scheduled_at=None, no estimate, no actual

    assert reviews.basis_for(trip)[0] == reviews.OvertimeBasis.TRANSFER_WAIT


def test_billable_minutes_default_to_the_suggestion_until_someone_decides():
    trip = _finished_trip()
    start = trip.pickup_at

    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=40),
    )

    assert review.suggested_overtime_minutes == 60  # 40 over, 15-min grace: an hour
    assert review.billable_overtime_minutes == 60
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

    assert review.suggested_overtime_minutes == 60
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
    assert review.suggested_overtime_minutes == 60


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
    assert f.suggested_minutes == 60  # 40 over, 15-min grace: a full hour
    assert f.customer_rate == Decimal("168")
    assert f.customer_amount == Decimal("168.00")  # 45 min bills the full hour at $168/h
    assert f.coverage == "affiliate"
    assert f.payout == Decimal("420")
    # $420 isn't what the trip's factor pays, so the share is payout ÷ subtotal ($504).
    assert f.affiliate_share_basis == "payout"
    assert f.affiliate_overtime_amount == Decimal("140.00")  # $168 × 420/504
    assert f.expected_affiliate_amount == Decimal("560.00")


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


def test_the_rule_copy_names_the_class_grace_and_the_hour():
    trip = _finished_trip(vehicle=_bus())
    review = _decided(trip, minutes_over=38)

    f = reviews.figures(review, Assignment.objects.active().filter(reservation=trip).first())

    assert (f.suggested_minutes, f.basis, f.grace_minutes) == (60, "hourly", 5)
    assert f.rule == "38 min over; 5-min grace for a coach, so 1 hour bills."


def test_the_rule_copy_inside_the_grace_and_for_two_hours():
    trip = _finished_trip(vehicle=_sedan())

    inside = reviews.figures(_decided(trip, minutes_over=12), None)
    assert inside.rule == "12 min over, inside the 15-min grace, so nothing to suggest."

    two = reviews.figures(_decided(trip, minutes_over=80), None)
    assert two.rule == "80 min over; 15-min grace for a sedan, so 2 hours bill."


def test_the_rule_copy_for_a_transfer_wait_and_an_arrival():
    waited = reviews.figures(_waited(_transfer(), 20), None)
    assert waited.rule == "20 min waiting at pickup; 15-min transfer grace, so 1 hour bills."

    trip = _transfer()
    landed = trip.pickup_at
    _arrival(trip, actual_at=landed)
    arrived = reviews.figures(_picked_up(trip, landed + timedelta(minutes=10)), None)
    assert arrived.rule == (
        "10 min after the flight's actual arrival, inside the 25-min domestic grace, "
        "so nothing to suggest."
    )


# --- the customer's overtime amount (APC-60) -----------------------------------------


def _billed(trip, minutes):
    return reviews.figures(
        _decided(trip, billable=minutes, minutes_over=minutes),
        Assignment.objects.active().filter(reservation=trip).first(),
    )


def test_overtime_is_billed_at_the_trips_rate_with_a_percentage_gratuity():
    trip = _finished_trip(rate=Decimal("200"), gratuity_pct=Decimal("20"))

    f = _billed(trip, 45)  # 45 min bills a full hour (APC-91): $200

    assert f.customer_amount == Decimal("200.00")  # base
    assert f.customer_overtime_gratuity == Decimal("40.00")
    assert f.customer_overtime_total == Decimal("240.00")


def test_a_flat_gratuity_adds_nothing_to_overtime():
    trip = _finished_trip(
        rate=Decimal("200"), gratuity_pct=Decimal("20"), gratuity_flat=Decimal("50")
    )

    f = _billed(trip, 45)

    assert f.customer_overtime_gratuity == Decimal("0.00")
    assert f.customer_overtime_total == Decimal("200.00")


def test_a_percentage_discount_applies_to_the_overtime_rate():
    trip = _finished_trip(
        rate=Decimal("200"), discount_pct=Decimal("10"), gratuity_pct=Decimal("20")
    )

    f = _billed(trip, 30)  # $200/h less 10% = $180/h; 30 min bills the hour = $180; 20% = $36

    assert f.customer_rate == Decimal("180.00")
    assert f.customer_amount == Decimal("180.00")
    assert f.customer_overtime_gratuity == Decimal("36.00")
    assert f.customer_overtime_total == Decimal("216.00")


def test_a_flat_discount_is_spread_over_the_billed_hours_for_overtime():
    # 3 h at $200 = $600, less $60 flat = $540 → $180/h; 30 min of overtime bills the hour.
    trip = _finished_trip(hours="3", rate=Decimal("200"), discount_flat=Decimal("60"))

    f = _billed(trip, 30)

    assert f.customer_rate == Decimal("180.00")
    assert f.customer_amount == Decimal("180.00")


def test_the_affiliates_overtime_follows_the_discounted_customer_amount():
    trip = _finished_trip(
        rate=Decimal("200"), discount_pct=Decimal("10"), affiliate_cost=Decimal("270")
    )
    Assignment.objects.active().filter(reservation=trip).update(payout=Decimal("270"))

    f = _billed(trip, 30)  # $180 billed (a full hour) × (270 ÷ 600 subtotal) = $81.00

    assert f.customer_amount == Decimal("180.00")
    assert f.affiliate_overtime_amount == Decimal("81.00")


def test_overtime_is_whole_hours_and_the_gratuity_rounds_to_the_cent():
    trip = _finished_trip(rate=Decimal("100.10"), gratuity_pct=Decimal("15"))

    f = _billed(trip, 15)  # 15 min bills the hour: $100.10; 15% of that = $15.015 → $15.02

    assert f.customer_amount == Decimal("100.10")
    assert f.customer_overtime_gratuity == Decimal("15.02")


def test_waived_overtime_totals_zero():
    trip = _finished_trip(rate=Decimal("200"), gratuity_pct=Decimal("20"))
    review = _decided(trip, billable=45, waived=True, reason="Traffic", minutes_over=45)

    f = reviews.figures(review, Assignment.objects.active().filter(reservation=trip).first())

    assert f.customer_overtime_total == Decimal("0.00")
