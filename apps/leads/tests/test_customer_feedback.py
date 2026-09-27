"""APC-63 — customer feedback on the trip page, and the future-booking follow-up."""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import CustomerFeedback, Lead
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.tasks import services as tasks
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(*, days_ago=1, trips=1, zone="America/Los_Angeles", status=""):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    for i in range(trips):
        trip = ReservationFactory(
            lead=lead,
            pickup_date=timezone.localdate() - timedelta(days=days_ago),
            pickup_time=time(10 + i, 0),
            pickup_timezone=zone,
            trip_status=status,
        )
        AssignmentFactory(reservation=trip, in_house=True)
    return lead


def _url(lead):
    trip = lead.reservations.first()
    token = ack.make_trip_day_ack_token(lead.contact, trip.pickup_date)
    return reverse("trip_confirm", args=[token])


def _post(client, lead, rating, comment=""):
    return client.post(
        _url(lead), {"form": "feedback", "lead": lead.pk, "rating": rating, "comment": comment}
    )


# --- the form ------------------------------------------------------------------------


def test_the_form_is_hidden_until_the_order_is_done(client):
    lead = _order(days_ago=-3)  # three days ahead

    body = client.get(_url(lead)).content.decode()

    assert 'name="rating"' not in body


def test_the_form_shows_once_every_trip_is_done(client):
    lead = _order(status=Reservation.TripStatus.DONE)

    body = client.get(_url(lead)).content.decode()

    assert 'name="rating"' in body
    assert "How was your trip?" in body


def test_one_trip_still_ahead_keeps_the_form_hidden(client):
    lead = _order(status=Reservation.TripStatus.DONE)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=2),
        pickup_time=time(9, 0),
        pickup_timezone="America/Los_Angeles",
    )

    assert 'name="rating"' not in client.get(_url(lead)).content.decode()


def test_the_token_is_required(client):
    lead = _order(status=Reservation.TripStatus.DONE)
    url = reverse("trip_confirm", args=["forged"])

    resp = client.post(url, {"form": "feedback", "lead": lead.pk, "rating": 5})

    assert resp.status_code == 404
    assert not CustomerFeedback.objects.exists()


def test_the_token_only_reaches_its_own_orders(client):
    mine = _order(status=Reservation.TripStatus.DONE)
    theirs = _order(status=Reservation.TripStatus.DONE)

    client.post(_url(mine), {"form": "feedback", "lead": theirs.pk, "rating": 1})

    assert not CustomerFeedback.objects.exists()


def test_submitting_before_the_trips_are_done_is_refused(client):
    lead = _order(days_ago=-3)

    _post(client, lead, 5)

    assert not CustomerFeedback.objects.exists()


def test_submitting_stores_one_row_and_resubmitting_edits_it(client):
    lead = _order(status=Reservation.TripStatus.DONE)

    assert _post(client, lead, 4, "Great driver").status_code == 302
    _post(client, lead, 5, "Great driver, spotless car")

    fb = CustomerFeedback.objects.get()
    assert (fb.lead, fb.rating, fb.comment) == (lead, 5, "Great driver, spotless car")
    assert fb.submitted_at is not None
    assert "Thanks for your feedback" in client.get(_url(lead)).content.decode()


@pytest.mark.parametrize("rating", ["0", "6", "", "five"])
def test_a_rating_outside_one_to_five_is_refused(client, rating):
    lead = _order(status=Reservation.TripStatus.DONE)

    resp = _post(client, lead, rating)

    assert resp.status_code == 200
    assert "Pick a rating" in resp.content.decode()
    assert not CustomerFeedback.objects.exists()


# --- the follow-up task --------------------------------------------------------------


@pytest.mark.parametrize(("rating", "opens"), [(1, True), (2, True), (3, False), (5, False)])
def test_a_low_rating_opens_a_customer_service_follow_up(client, rating, opens):
    lead = _order(status=Reservation.TripStatus.DONE)

    _post(client, lead, rating)

    task = Task.objects.filter(lead=lead, kind="feedback_followup").first()
    assert (task is not None) is opens
    if task:
        assert task.department == "customer_service"
        assert task.reservation is None
        assert task.status == Task.Status.OPEN


def test_the_workspace_and_order_page_show_the_feedback(client):
    lead = _order(status=Reservation.TripStatus.DONE)
    _post(client, lead, 2, "Driver was 20 minutes late")
    client.force_login(UserFactory())

    for name in ("lead_detail", "order_detail"):
        arg = {"pk": lead.pk} if name == "lead_detail" else {"lead_id": lead.pk}
        body = client.get(reverse(name, kwargs=arg)).content.decode()
        assert "Driver was 20 minutes late" in body, name
        assert "2 / 5" in body, name


# --- future booking follow-up --------------------------------------------------------


def _through_stage_three(lead):
    run_tasks()
    user = UserFactory()
    for kind in ("ops_review", "overtime_invoiced", "thank_you_sent"):
        for task in Task.objects.filter(lead=lead, kind=kind, status__in=Task.UNRESOLVED):
            tasks.complete(task, user)


def test_future_booking_waits_for_every_trips_stage_three():
    lead = _order(trips=2, status=Reservation.TripStatus.DONE)
    run_tasks()
    first, _second = lead.reservations.order_by("pk")
    user = UserFactory()
    for kind in ("ops_review", "overtime_invoiced", "thank_you_sent"):
        tasks.complete(Task.objects.get(reservation=first, kind=kind), user)
    assert not Task.objects.filter(lead=lead, kind="future_booking_followup").exists()

    _through_stage_three(lead)

    task = Task.objects.get(lead=lead, kind="future_booking_followup")
    assert task.reservation is None
    assert task.department == "sales"
    assert task.status == Task.Status.OPEN


def test_future_booking_is_due_the_offset_after_the_last_trip_in_its_zone():
    lead = _order(trips=2, status=Reservation.TripStatus.DONE, zone="America/Los_Angeles")
    last = lead.reservations.order_by("-pickup_time").first()

    _through_stage_three(lead)

    task = Task.objects.with_order_tz().get(lead=lead, kind="future_booking_followup")
    la = ZoneInfo("America/Los_Angeles")
    expected = datetime.combine(last.pickup_date + timedelta(days=330), time(9, 0), tzinfo=la)
    assert task.due_at == expected.astimezone(UTC)
    assert task.due_display.endswith(("PDT", "PST"))


def test_the_offset_is_a_setting():
    cfg = TaskConfig.load()
    cfg.future_booking_offset_days = 30
    cfg.save()
    lead = _order(status=Reservation.TripStatus.DONE)
    trip = lead.reservations.get()

    _through_stage_three(lead)

    task = Task.objects.get(lead=lead, kind="future_booking_followup")
    local = task.due_at.astimezone(ZoneInfo("America/Los_Angeles"))
    assert local.date() == trip.pickup_date + timedelta(days=30)


def test_future_booking_is_manual_and_keeps_its_note():
    lead = _order(status=Reservation.TripStatus.DONE)
    _through_stage_three(lead)
    task = Task.objects.get(lead=lead, kind="future_booking_followup")

    run_tasks()
    task.refresh_from_db()
    assert task.status == Task.Status.OPEN

    tasks.complete(task, UserFactory(), note="Booked their 1st anniversary dinner run")
    task.refresh_from_db()
    assert task.note == "Booked their 1st anniversary dinner run"
