"""The two post-trip email reports: customer reviews (monthly) and new complaints (daily)."""

from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.core.cron import JOBS
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import CustomerFeedback, Lead
from apps.messaging.factories import ReviewFactory, TouchPointFactory
from apps.messaging.models import TouchPoint
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.tasks import reports
from apps.tasks.models import Task, TaskConfig
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

_K = TouchPoint.Kind


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.PUBLIC_BASE_URL = "https://apc.example"
    cfg = TaskConfig.load()
    cfg.report_emails = "owner@allprocharter.com"
    cfg.save()
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(*, days_ago=3, status=Reservation.TripStatus.DONE, name="Dana Reyes"):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    lead.contact.name = name
    lead.contact.save()
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
        trip_status=status,
    )
    return lead, trip


def _rate(lead, rating, comment=""):
    CustomerFeedback.objects.create(
        lead=lead, rating=rating, comment=comment, submitted_at=timezone.now()
    )


def _complain(trip, note="Driver was 40 minutes late", category=TripIssue.Category.COMPLAINT):
    return reviews.add_issue(
        reviews.review_for(trip),
        category=category,
        severity=TripIssue.Severity.HIGH,
        note=note,
        user=UserFactory(first_name="Moe", last_name="A"),
    )


# --- customer review report ------------------------------------------------------------


def test_both_reports_are_cron_jobs():
    assert JOBS["review-report"] is reports.send_review_report
    assert JOBS["complaint-report"] is reports.send_complaint_report


def test_it_lists_each_finished_order_with_what_came_back():
    asked, _ = _order(name="Asked Only")
    TouchPointFactory(lead=asked, kind=_K.REVIEW_REQUEST, status=TouchPoint.Status.SENT)
    ReviewFactory(lead=asked, contact=asked.contact)
    reviewed, _ = _order(name="Left Review")
    TouchPointFactory(lead=reviewed, kind=_K.REVIEW_REQUEST, status=TouchPoint.Status.SENT)
    ReviewFactory(lead=reviewed, contact=reviewed.contact, rating=5, review_site="Google")
    _rate(reviewed, 5)
    held, _ = _order(name="Held Back")
    TouchPointFactory(
        lead=held,
        kind=_K.REVIEW_REQUEST,
        status=TouchPoint.Status.CANCELLED,
        error="Review request held: low rating",
    )
    _rate(held, 1, "Never again")
    _order(name="Nobody Asked")

    now = timezone.now()
    rows = {r["customer"]: r for r in reports.review_report_rows(now - timedelta(days=31), now)}

    assert (rows["Asked Only"]["asked"], rows["Asked Only"]["review"]) == ("Asked", "No review yet")
    assert rows["Left Review"]["review"] == "5-star review on Google"
    assert rows["Left Review"]["reviewed"] and rows["Left Review"]["rating"] == 5
    assert rows["Held Back"]["asked"] == "Held: low rating"
    assert rows["Held Back"]["low"] and rows["Held Back"]["comment"] == "Never again"
    assert (rows["Nobody Asked"]["asked"], rows["Nobody Asked"]["review"]) == (
        "Not asked",
        "No review",
    )


def test_orders_still_running_or_cancelled_are_left_out():
    _order(name="Done")
    _order(name="Ahead", days_ago=-2, status="")
    lost, _ = _order(name="Lost")
    Lead.objects.filter(pk=lost.pk).update(status=Lead.Status.LOST)

    now = timezone.now()
    rows = reports.review_report_rows(now - timedelta(days=31), now)

    assert [r["customer"] for r in rows] == ["Done"]


def test_the_email_leads_with_the_numbers_and_calls_out_low_ratings():
    good, _ = _order(name="Happy Customer")
    ReviewFactory(lead=good, contact=good.contact, rating=5)
    bad, _ = _order(name="Unhappy Customer")
    _rate(bad, 2, "Bus was dirty")

    assert reports.send_review_report() == 2

    message = mail.outbox[0]
    assert message.to == ["owner@allprocharter.com"]
    assert message.subject == "Customer reviews: 2 orders finished, 1 reviewed"
    assert "2 orders finished" in message.body
    assert "Low ratings (1 or 2 stars)" in message.body
    assert "Unhappy Customer · 2 of 5" in message.body and "Bus was dirty" in message.body
    assert "https://apc.example" in message.body


def test_the_next_report_starts_where_the_last_one_stopped():
    _order(name="First Month")
    assert reports.send_review_report() == 1

    assert reports.send_review_report() == 0  # nothing new: nothing sent
    assert len(mail.outbox) == 1

    lead, trip = _order(name="Second Month", days_ago=0, status="")
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.DONE)
    later = timezone.now() + timedelta(hours=1)
    assert reports.send_review_report(now=later) == 1
    assert "Second Month" in mail.outbox[1].body and "First Month" not in mail.outbox[1].body


def test_the_first_report_looks_back_a_month():
    _order(name="Recent", days_ago=20)
    _order(name="Old", days_ago=45)

    assert reports.send_review_report() == 1
    assert "Recent" in mail.outbox[0].body and "Old" not in mail.outbox[0].body


def test_the_review_report_costs_the_same_for_many_orders():
    now = timezone.now()
    _order()
    with CaptureQueriesContext(connection) as few:
        reports.review_report_rows(now - timedelta(days=31), now)
    for _ in range(15):
        lead, _ = _order()
        _rate(lead, 4)
    with CaptureQueriesContext(connection) as many:
        reports.review_report_rows(now - timedelta(days=31), now)

    assert len(many) == len(few) <= 7


def test_a_low_rating_no_longer_opens_a_task():
    lead, _ = _order()
    from apps.leads.services import record_feedback

    record_feedback(lead, rating=1, comment="x")

    assert not Task.objects.filter(lead=lead, kind="feedback_followup").exists()


# --- complaint report ------------------------------------------------------------------


def test_nothing_goes_out_on_a_day_without_a_new_complaint():
    _, trip = _order()
    _complain(trip, category=TripIssue.Category.VEHICLE)

    assert reports.send_complaint_report() == 0
    assert mail.outbox == []
    assert TaskConfig.load().complaint_report_through is not None


def test_new_complaints_go_out_with_the_incident_numbers():
    lead, trip = _order(name="Dana Reyes")
    AssignmentFactory(
        reservation=trip,
        vendor=VendorFactory(name="Metro Limo"),
        status=Assignment.Status.CONFIRMED,
    )
    _complain(trip)
    _complain(trip, note="AC was broken", category=TripIssue.Category.VEHICLE)
    _, other = _order(name="Sam Ortiz")
    _complain(other, note="Wrong pickup spot")

    assert reports.send_complaint_report() == 2

    message = mail.outbox[0]
    assert message.subject == "2 new customer complaints"
    assert message.to == ["owner@allprocharter.com"]
    body = message.body
    assert "Dana Reyes" in body and "Driver was 40 minutes late" in body
    assert "Metro Limo" in body and "Logged by Moe A" in body
    assert "AC was broken" not in body  # not a complaint
    assert "Customer complaint 2" in body and "Vehicle 1" in body
    assert "Open now: 3 issues, 2 of them complaints" in body
    assert "Complaints in the last 30 days: 2" in body
    assert f"https://apc.example/portal/trip-review/{lead.pk}/" in body


def test_a_complaint_is_reported_once():
    _, trip = _order()
    _complain(trip)
    assert reports.send_complaint_report() == 1

    assert reports.send_complaint_report() == 0
    assert len(mail.outbox) == 1

    _complain(trip, note="And the driver was rude")
    assert reports.send_complaint_report(now=timezone.now() + timedelta(minutes=1)) == 1
    assert "rude" in mail.outbox[1].body and "40 minutes late" not in mail.outbox[1].body


def test_reports_fall_back_to_the_digest_recipients():
    cfg = TaskConfig.load()
    cfg.report_emails = ""
    cfg.digest_emails = "ops@allprocharter.com"
    cfg.save()
    _, trip = _order()
    _complain(trip)

    reports.send_complaint_report()

    assert mail.outbox[0].to == ["ops@allprocharter.com"]


# --- the go-live switch (APC-90) --------------------------------------------------------


def test_the_review_report_still_reaches_the_office_before_go_live(settings):
    settings.OUTBOUND_ENABLED = False
    _order(name="Dana Reyes")

    reports.send_review_report()

    assert [m.to for m in mail.outbox] == [["owner@allprocharter.com"]]


def test_the_complaint_report_still_reaches_the_office_before_go_live(settings):
    settings.OUTBOUND_ENABLED = False
    _, trip = _order(name="Dana Reyes")
    _complain(trip)

    reports.send_complaint_report()

    assert [m.to for m in mail.outbox] == [["owner@allprocharter.com"]]
