"""APC-59 — the Trip Review pages and the trip review endpoints."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue, TripReview
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _order(*, farmed_out=True, status=Reservation.TripStatus.DONE, zone="America/Los_Angeles"):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(22, 0),
        pickup_timezone=zone,
        rate=Decimal("168"),
        hours=Decimal("3"),
        trip_status=status,
    )
    if farmed_out:
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED, payout=420)
    else:
        AssignmentFactory(reservation=trip, in_house=True)
    PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    run_tasks()
    return lead, trip


def _trip_url(name, trip):
    return reverse(name, args=[trip.pk])


# --- list ----------------------------------------------------------------------------


def test_the_list_needs_a_login(client):
    assert client.get(reverse("trip_review_list")).status_code == 302


def test_the_list_shows_orders_in_review(staff):
    lead, _trip = _order()

    body = staff.get(reverse("trip_review_list")).content.decode()

    assert lead.quote_no in body
    assert "0 of 1 reviewed" in body
    assert "Needs ops review" in body
    assert "Total due" in body and "Remaining" in body
    assert "data-tom" in body  # filters are Tom Select, never a bare <select>
    assert "<dialog" not in body


def test_the_sidebar_has_a_trip_review_entry(staff):
    body = staff.get(reverse("dashboard")).content.decode()

    assert reverse("trip_review_list") in body
    assert "Trip Review" in body


def test_the_list_page_costs_the_same_for_two_orders_or_eight(staff):
    for _ in range(2):
        _order()
    with CaptureQueriesContext(connection) as few:
        staff.get(reverse("trip_review_list"))
    for _ in range(6):
        _order()
    with CaptureQueriesContext(connection) as many:
        staff.get(reverse("trip_review_list"))

    assert len(many) == len(few)


# --- order ---------------------------------------------------------------------------


def test_the_order_page(staff):
    lead, trip = _order()
    cancelled = ReservationFactory(
        lead=lead,
        pickup_date=trip.pickup_date,
        pickup_time=time(9, 0),
        pickup_timezone="America/Los_Angeles",
        trip_status=Reservation.TripStatus.CANCELLED,
    )

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert lead.quote_no in body
    assert "Trips reviewed" in body and "0 / 1" in body
    assert "Needs review" in body
    assert "Cancelled · nothing to review" in body
    assert "Final billing" in body and "Remaining to collect" in body
    assert _trip_url("trip_review_trip", trip) in body
    assert _trip_url("trip_review_trip", cancelled) not in body


def test_a_trip_with_no_status_says_it_ended_by_schedule(staff):
    lead, _trip = _order(status="")

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "No status · ended by schedule" in body


def test_a_reviewed_trip_shows_its_billed_overtime(staff):
    lead, trip = _order()
    start = trip.pickup_at
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=30),
        billable_overtime_minutes=30,
    )
    reviews.complete_review(review, user=UserFactory())

    body = staff.get(reverse("trip_review_order", args=[lead.pk])).content.decode()

    assert "Reviewed · 30 min OT billed ($84.00)" in body


def test_an_order_not_in_review_404s(staff):
    lead = LeadFactory(status=Lead.Status.BOOKED)

    assert staff.get(reverse("trip_review_order", args=[lead.pk])).status_code == 404


# --- the trip review pop-up ----------------------------------------------------------


def test_the_trip_review_shows_both_panes_in_the_trips_zone(staff):
    _lead, trip = _order()

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Customer billing" in body and "Driver pay" in body
    assert "Affiliate overtime" in body and "Expected to pay" in body
    assert "Approve payable" not in body  # a plain agent can't approve
    assert "PDT" in body or "PST" in body
    assert "client to confirm" in body


def test_an_in_house_trip_shows_driver_pay_and_no_payable(staff):
    _lead, trip = _order(farmed_out=False)

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Total time on the job" in body
    driver = trip.assignments.get().driver
    assert f"Set an hourly rate on {driver.name}" in body
    assert reverse("fleet:driver_edit", args=[driver.pk]) in body
    assert "Affiliate overtime" not in body
    assert "Payable" not in body


def test_a_payments_user_can_work_the_payable_in_the_pane(client):
    client.force_login(UserFactory(can_manage_payments=True))
    _lead, trip = _order()

    body = client.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert "Invoice missing" in body or "invoice_missing" in body
    assert "Approve payable" in body and "Mark paid" in body
    assert "Save invoice" in body


def test_a_cancelled_trip_has_nothing_to_review(staff):
    lead, _trip = _order()
    cancelled = ReservationFactory(lead=lead, trip_status=Reservation.TripStatus.CANCELLED)

    assert staff.get(_trip_url("trip_review_trip", cancelled)).status_code == 404


def test_a_trip_whose_order_isnt_in_review_404s(staff):
    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))

    assert staff.get(_trip_url("trip_review_trip", trip)).status_code == 404


def test_saving_times_entered_in_the_trips_zone(staff):
    _lead, trip = _order()
    day = trip.pickup_date

    resp = staff.post(
        _trip_url("trip_review_save", trip),
        {
            "pickup_date": day.isoformat(),
            "pickup_time": "22:05",
            "dropoff_date": (day + timedelta(days=1)).isoformat(),
            "dropoff_time": "01:45",
        },
    )

    assert resp.status_code == 200
    data = resp.json()["review"]
    review = TripReview.objects.get(reservation=trip)
    local = review.actual_dropoff_local
    assert (local.date(), local.hour, local.minute) == (day + timedelta(days=1), 1, 45)
    assert data["actual_minutes"] == 220
    assert data["suggested_minutes"] == 30


def test_half_a_time_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_save", trip), {"pickup_date": "2026-09-25"})

    assert resp.status_code == 400
    assert "date and the time" in resp.json()["error"]


def test_saving_the_customer_decision_and_driver_pay_separately(staff):
    _lead, trip = _order()
    url = _trip_url("trip_review_save", trip)
    start = trip.pickup_at
    review = reviews.review_for(trip)
    reviews.save_review(
        review,
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3, minutes=40),
    )

    staff.post(url, {"billable_minutes": "30", "waived": "0"})
    resp = staff.post(url, {"override": "60", "override_note": "Waited at the FBO"})

    data = resp.json()["review"]
    assert data["billable_minutes"] == 30
    assert data["customer_amount"] == "84.00"
    # 30 billed min × the payout share would be $70; the adjustment replaces it.
    assert data["affiliate_overtime_derived"] == "70.00"
    assert data["affiliate_overtime_overridden"] is True
    assert data["expected_affiliate_amount"] == "480.00"
    assert data["decided"] is True


def test_an_adjustment_without_a_note_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_save", trip), {"override": "60"})

    assert resp.status_code == 400
    assert "note" in resp.json()["error"]


def test_waiving_without_a_reason_is_refused(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_save", trip), {"billable_minutes": "30", "waived": "1"}
    )

    assert resp.status_code == 400
    assert "reason" in resp.json()["error"]


def test_rating_the_affiliate(staff):
    _lead, trip = _order()

    staff.post(_trip_url("trip_review_save", trip), {"rating": "4"})

    assert TripReview.objects.get(reservation=trip).affiliate_rating == 4


def test_completing_from_the_pop_up(staff):
    _lead, trip = _order()
    start = trip.pickup_at
    reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=start + timedelta(hours=3),
        billable_overtime_minutes=0,
    )

    resp = staff.post(_trip_url("trip_review_complete", trip))

    assert resp.status_code == 200
    assert Task.objects.get(reservation=trip, kind="ops_review").status == Task.Status.DONE


def test_completing_without_a_decision_explains_why(staff):
    _lead, trip = _order()

    resp = staff.post(_trip_url("trip_review_complete", trip))

    assert resp.status_code == 400
    assert "actual" in resp.json()["error"]


def test_logging_and_resolving_an_issue(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_issue_add", trip),
        {"category": "complaint", "severity": "high", "note": "Left early"},
    )
    assert resp.status_code == 200
    issue = TripIssue.objects.get()

    resp = staff.post(reverse("trip_review_issue_resolve", args=[issue.pk]))

    assert resp.status_code == 200
    issue.refresh_from_db()
    assert issue.resolved_at is not None


def test_an_issue_needs_a_note(staff):
    _lead, trip = _order()

    resp = staff.post(
        _trip_url("trip_review_issue_add", trip), {"category": "driver", "severity": "low"}
    )

    assert resp.status_code == 400


def test_the_saved_times_come_back_in_the_form(staff):
    _lead, trip = _order()
    reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=datetime(2026, 9, 26, 5, 5, tzinfo=UTC),  # 10:05 PM PDT
        actual_dropoff_at=datetime(2026, 9, 26, 8, 45, tzinfo=UTC),
    )

    body = staff.get(_trip_url("trip_review_trip", trip)).content.decode()

    assert 'value="22:05"' in body and 'value="01:45"' in body
