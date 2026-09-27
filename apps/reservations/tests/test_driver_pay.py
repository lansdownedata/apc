"""APC-61 — driver pay on the trip review: affiliate overtime rises with the customer's
billed overtime, and an in-house driver is paid total trip time × their hourly rate."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.tasks.jobs import run_tasks

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _trip(*, driver=None, payout=None, **extra):
    """A 3-hour trip at $200/h (subtotal $600) that ran yesterday, covered by an affiliate
    on the 70% factor unless `driver` puts one of our own drivers on it."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    fields = {"rate": Decimal("200"), "hours": Decimal("3"), "cost_ratio_pct": Decimal("70")}
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(18, 0),
        pickup_timezone="America/New_York",
        **{**fields, **extra},
    )
    if driver is not None:
        AssignmentFactory(reservation=trip, in_house=True, driver=driver)
    else:
        AssignmentFactory(
            reservation=trip,
            status=Assignment.Status.CONFIRMED,
            payout=trip.vendor_pay if payout is None else payout,
        )
    run_tasks()
    return trip


def _review(trip, *, billable=0, waived=False, minutes_over=0):
    start = trip.pickup_at
    return reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=float(trip.billed_hours), minutes=minutes_over),
        billable_overtime_minutes=billable,
        overtime_waived=waived,
        waive_reason="Traffic" if waived else "",
    )


def _coverage(trip):
    return Assignment.objects.active().filter(reservation=trip).select_related("driver").first()


def _figures(trip, review):
    review.refresh_from_db()
    return reviews.figures(review, _coverage(trip))


# --- affiliate overtime rises with the bill ------------------------------------------


def test_a_factor_trip_shares_overtime_at_its_factor():
    trip = _trip()  # payout = 70% of $600 = $420
    f = _figures(trip, _review(trip, billable=45, minutes_over=40))

    assert f.customer_amount == Decimal("150.00")  # 45 min at $200/h
    assert f.affiliate_share == Decimal("0.70")
    assert f.affiliate_share_basis == "factor"
    assert f.affiliate_overtime_derived == Decimal("105.00")
    assert f.affiliate_overtime_amount == Decimal("105.00")
    assert f.expected_affiliate_amount == Decimal("525.00")  # $420 + $105


def test_a_flat_trip_shares_overtime_at_payout_over_subtotal():
    trip = _trip(affiliate_cost=Decimal("450"))  # flat $450 on a $600 subtotal = 75%
    f = _figures(trip, _review(trip, billable=30, minutes_over=30))

    assert f.affiliate_share_basis == "payout"
    assert f.affiliate_share == Decimal("0.75")
    assert f.affiliate_overtime_amount == Decimal("75.00")  # $100 billed × 75%
    assert f.expected_affiliate_amount == Decimal("525.00")


def test_a_separately_agreed_payout_uses_the_effective_share():
    """On the factor, but the dispatcher agreed $300 instead of the $420 the factor says."""
    trip = _trip(payout=Decimal("300"))
    f = _figures(trip, _review(trip, billable=60, minutes_over=60))

    assert f.affiliate_share_basis == "payout"
    assert f.affiliate_share == Decimal("0.50")
    assert f.affiliate_overtime_amount == Decimal("100.00")  # $200 billed × 50%


def test_waived_overtime_pays_the_affiliate_no_overtime():
    trip = _trip()
    f = _figures(trip, _review(trip, billable=45, waived=True, minutes_over=40))

    assert f.customer_amount == Decimal("0.00")
    assert f.affiliate_overtime_amount == Decimal("0.00")
    assert f.expected_affiliate_amount == Decimal("420.00")


def test_zero_billed_overtime_pays_no_overtime():
    trip = _trip()
    f = _figures(trip, _review(trip, billable=0, minutes_over=10))

    assert f.affiliate_overtime_amount == Decimal("0.00")


def test_gratuity_is_never_shared():
    """A percentage gratuity rides on the customer's overtime (APC-60), but the affiliate's
    share is of the base only — gratuity is a pass-through, the `vendor_pay` rule."""
    trip = _trip(gratuity_pct=Decimal("20"))
    f = _figures(trip, _review(trip, billable=45, minutes_over=40))

    assert f.affiliate_overtime_amount == Decimal("105.00")  # 70% of $150, not of $180


def test_the_override_replaces_the_derived_amount_and_needs_a_note():
    trip = _trip()
    review = _review(trip, billable=0, waived=True, minutes_over=40)

    with pytest.raises(reviews.ReviewError, match="note"):
        reviews.save_review(review, user=UserFactory(), affiliate_overtime_override=Decimal("90"))

    reviews.save_review(
        review,
        user=UserFactory(),
        affiliate_overtime_override=Decimal("90"),
        affiliate_overtime_note="Customer waived, but the chauffeur still worked 40 min",
    )
    f = _figures(trip, review)

    assert f.affiliate_overtime_overridden is True
    assert f.affiliate_overtime_derived == Decimal("0.00")
    assert f.affiliate_overtime_amount == Decimal("90.00")
    assert f.expected_affiliate_amount == Decimal("510.00")


def test_clearing_the_override_goes_back_to_the_derived_amount():
    trip = _trip()
    review = _review(trip, billable=45, minutes_over=40)
    reviews.save_review(
        review,
        user=UserFactory(),
        affiliate_overtime_override=Decimal("10"),
        affiliate_overtime_note="Agreed on the phone",
    )

    reviews.save_review(review, user=UserFactory(), affiliate_overtime_override=None)
    f = _figures(trip, review)

    assert f.affiliate_overtime_overridden is False
    assert f.affiliate_overtime_amount == Decimal("105.00")
    assert review.affiliate_overtime_note == ""


def test_a_negative_override_is_refused():
    trip = _trip()
    with pytest.raises(reviews.ReviewError):
        reviews.save_review(
            reviews.review_for(trip),
            user=UserFactory(),
            affiliate_overtime_override=Decimal("-1"),
            affiliate_overtime_note="x",
        )


# --- in-house driver pay: total time × hourly rate -----------------------------------


def test_in_house_pay_is_total_trip_time_times_the_rate():
    driver = DriverFactory(hourly_rate=Decimal("30"))
    trip = _trip(driver=driver)
    f = _figures(trip, _review(trip, minutes_over=30))  # 3h30 on the job

    assert f.coverage == "in_house"
    assert f.expected_affiliate_amount is None
    assert f.driver_hourly_rate == Decimal("30")
    assert f.driver_pay_amount == Decimal("105.00")  # 210 min / 60 × $30


def test_in_house_pay_ignores_what_the_customer_is_billed():
    driver = DriverFactory(hourly_rate=Decimal("30"))
    trip = _trip(driver=driver)
    f = _figures(trip, _review(trip, billable=0, waived=True, minutes_over=30))

    assert f.driver_pay_amount == Decimal("105.00")


def test_a_driver_with_no_rate_has_no_pay_figure_not_zero():
    trip = _trip(driver=DriverFactory(hourly_rate=Decimal("0")))
    f = _figures(trip, _review(trip, minutes_over=30))

    assert f.driver_hourly_rate is None
    assert f.driver_pay_amount is None


def test_no_actual_times_means_no_pay_figure_yet():
    trip = _trip(driver=DriverFactory(hourly_rate=Decimal("30")))
    f = reviews.figures(reviews.review_for(trip), _coverage(trip))

    assert f.driver_pay_amount is None


def test_completing_stamps_the_pay_and_a_later_rate_edit_leaves_it_alone():
    driver = DriverFactory(hourly_rate=Decimal("30"))
    trip = _trip(driver=driver)
    review = _review(trip, minutes_over=30)

    reviews.complete_review(review, user=UserFactory())
    driver.hourly_rate = Decimal("45")
    driver.save()

    review.refresh_from_db()
    assert review.driver_hourly_rate == Decimal("30.00")
    assert review.driver_pay_amount == Decimal("105.00")
    f = _figures(trip, review)
    assert f.driver_hourly_rate == Decimal("30.00")
    assert f.driver_pay_amount == Decimal("105.00")


def test_completing_a_farmed_out_trip_stamps_no_driver_pay():
    trip = _trip()
    review = _review(trip, minutes_over=30)

    reviews.complete_review(review, user=UserFactory())

    review.refresh_from_db()
    assert review.driver_pay_amount is None
    assert review.driver_hourly_rate is None
