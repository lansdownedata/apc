"""APC-70 — the affiliate performance scorecard's numbers."""

from datetime import time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue, TripReview, TripStatusEvent
from apps.vendors import performance
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")
TODAY = timezone.localdate()
START, END = TODAY - timedelta(days=30), TODAY


def _trip(
    vendor, *, days_ago=5, payout="100", vehicle=None, status=Assignment.Status.CONFIRMED, **a
):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    extra = {"vehicle": vehicle} if vehicle else {}
    trip = ReservationFactory(
        lead=lead,
        pickup_date=TODAY - timedelta(days=days_ago),
        pickup_time=time(12, 0),
        pickup_timezone="America/New_York",
        **extra,
    )
    assignment = AssignmentFactory(
        reservation=trip, vendor=vendor, status=status, payout=Decimal(payout), **a
    )
    return trip, assignment


def _card(vendor, **kw):
    kw.setdefault("min_trips", 1)
    return next(c for c in performance.scorecards(START, END, **kw) if c.vendor == vendor)


# --- each metric -----------------------------------------------------------------------


def test_confirmation_time_is_the_median_offer_to_acknowledgement():
    vendor = VendorFactory()
    for hours in (1, 3, 20):
        offered = timezone.now() - timedelta(days=6)
        _trip(vendor, offered_at=offered, affiliate_confirmed_at=offered + timedelta(hours=hours))
    _trip(vendor)  # never acknowledged: left out, not counted as zero

    card = _card(vendor)

    assert card.trips == 4
    assert card.confirm_hours == pytest.approx(3)


def test_driver_info_lead_time_is_the_median_hours_before_pickup():
    vendor = VendorFactory()
    for hours in (2, 10, 48):
        trip, assignment = _trip(vendor)
        Assignment.objects.filter(pk=assignment.pk).update(
            driver_info_at=trip.pickup_at - timedelta(hours=hours)
        )
    _trip(vendor)  # no stamp: excluded

    assert _card(vendor).driver_info_hours == pytest.approx(10)


def test_cancellation_rate_is_affiliate_cancellations_over_trips_held():
    vendor = VendorFactory()
    for _ in range(3):
        _trip(vendor)
    trip, _ = _trip(vendor, status=Assignment.Status.WITHDRAWN)
    TripStatusEvent.objects.create(
        reservation=trip, status=Reservation.TripStatus.CANCELLED_BY_AFFILIATE
    )

    card = _card(vendor)

    assert (card.trips, card.cancellations) == (3, 1)
    assert card.cancellation_rate == pytest.approx(0.25)


def test_issues_complaints_and_rating_come_from_the_trip_reviews():
    vendor = VendorFactory()
    trips = [_trip(vendor)[0] for _ in range(4)]
    user = UserFactory()
    for trip, rating in zip(trips[:3], (5, 4, 3), strict=True):
        review = reviews.review_for(trip)
        TripReview.objects.filter(pk=review.pk).update(affiliate_rating=rating)
    review = reviews.review_for(trips[0])
    for category in (TripIssue.Category.COMPLAINT, TripIssue.Category.VEHICLE):
        reviews.add_issue(
            review, category=category, severity=TripIssue.Severity.LOW, note="x", user=user
        )

    card = _card(vendor)

    assert (card.issues, card.complaints) == (2, 1)
    assert card.issues_per_trip == pytest.approx(0.5)
    assert (card.rating, card.ratings) == (pytest.approx(4), 3)


def test_pricing_compares_each_payout_to_its_vehicle_types_median():
    sedan, coach = VehicleTypeFactory(name="Sedan"), VehicleTypeFactory(name="Motor coach")
    cheap, dear = VendorFactory(), VendorFactory()
    _trip(cheap, payout="100", vehicle=sedan)
    _trip(dear, payout="120", vehicle=sedan)
    _trip(dear, payout="110", vehicle=sedan)
    _trip(dear, payout="900", vehicle=coach)  # the only coach: it is its own median

    # Sedan median is 110: 100/110, and (120/110 + 110/110 + 900/900) / 3.
    assert _card(cheap).price_ratio == pytest.approx(100 / 110)
    assert _card(dear).price_ratio == pytest.approx((120 / 110 + 1 + 1) / 3)


# --- the score -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "best", "worst", "expected"),
    [(1, 1, 24, 100), (24, 1, 24, 0), (12.5, 1, 24, 50), (40, 1, 24, 0), (0.05, 0, 0.2, 75)],
)
def test_a_sub_score_is_a_straight_line_between_best_and_worst(value, best, worst, expected):
    assert performance._scale(value, best=best, worst=worst) == pytest.approx(expected)


def test_the_composite_uses_the_weights_and_skips_what_is_missing():
    parts = {"cancellations": 100.0, "rating": 50.0}

    assert performance.composite(parts, {"cancellations": 3, "rating": 1}) == pytest.approx(87.5)
    assert performance.composite(parts, {"cancellations": 1, "rating": 1, "pricing": 8}) == 75
    assert performance.composite({}, {"rating": 1}) is None


def test_custom_weights_change_the_score():
    vendor = VendorFactory()
    trip, _ = _trip(vendor)
    TripReview.objects.filter(pk=reviews.review_for(trip).pk).update(affiliate_rating=3)

    only_rating = _card(vendor, weights={"rating": 1})
    only_cancels = _card(vendor, weights={"cancellations": 1})

    assert only_rating.score == pytest.approx(50)
    assert only_cancels.score == pytest.approx(100)


def test_no_score_until_there_are_enough_trips(settings):
    settings.AFFILIATE_SCORE_MIN_TRIPS = 5
    few, many = VendorFactory(), VendorFactory()
    for _ in range(4):
        _trip(few)
    for _ in range(5):
        _trip(many)

    cards = {c.vendor: c for c in performance.scorecards(START, END)}

    assert (cards[few].enough, cards[few].score) == (False, None)
    assert cards[few].trips == 4  # the raw numbers still show
    assert cards[many].enough and cards[many].score is not None


# --- the window, the list, the cost ----------------------------------------------------


def test_only_pickups_inside_the_window_count():
    vendor = VendorFactory()
    _trip(vendor, days_ago=5)
    _trip(vendor, days_ago=30)  # the first day of the window
    _trip(vendor, days_ago=31)
    _trip(vendor, days_ago=-3)  # still ahead

    assert _card(vendor).trips == 2


def test_in_house_trips_and_inactive_affiliates_are_left_out():
    active, inactive = VendorFactory(), VendorFactory(status=Vendor.Status.INACTIVE)
    _trip(active)
    _trip(inactive)
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=lead, pickup_date=TODAY - timedelta(days=2))
    AssignmentFactory(reservation=trip, in_house=True)

    cards = performance.scorecards(START, END, min_trips=1)

    assert [c.vendor for c in cards] == [active]
    assert cards[0].trips == 1


def test_ranking_puts_the_worst_first_and_the_unscored_last():
    good, bad, new = (
        VendorFactory(name="Good"),
        VendorFactory(name="Bad"),
        VendorFactory(name="New"),
    )
    for vendor, rating in ((good, 5), (bad, 2)):
        for _ in range(2):
            trip, _ = _trip(vendor)
            TripReview.objects.filter(pk=reviews.review_for(trip).pk).update(
                affiliate_rating=rating
            )
    _trip(new)

    cards = performance.scorecards(START, END, min_trips=2)

    assert [c.vendor.name for c in performance.ranked(cards)] == ["Bad", "Good", "New"]
    assert [c.vendor.name for c in performance.ranked(cards, worst_first=False)] == [
        "Good",
        "Bad",
        "New",
    ]


def test_the_cost_is_the_same_for_thirty_affiliates():
    _trip(VendorFactory())
    with CaptureQueriesContext(connection) as few:
        performance.scorecards(START, END)
    for _ in range(30):
        vendor = VendorFactory()
        for _ in range(2):
            trip, _ = _trip(vendor)
            reviews.review_for(trip)
    with CaptureQueriesContext(connection) as many:
        performance.scorecards(START, END)

    assert len(many) == len(few) <= 6
