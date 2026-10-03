"""APC-62 — the post-trip thank-you, and holding the review request after a complaint."""

from datetime import time, timedelta
from itertools import count
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.leads.services import record_feedback
from apps.messaging import touchpoints
from apps.messaging.models import Review, TouchPoint
from apps.messaging.touchpoint_templates import TEMPLATES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations import acknowledgements as ack
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.tasks import services as tasks
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

_K = TouchPoint.Kind
SEND = "apps.messaging.touchpoints.podium.send_message"
INVITE = "apps.messaging.touchpoints.podium.create_review_invitation"
_n = count(1)


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example"


def _order(*, trips=1, status=Reservation.TripStatus.DONE, wedding=False, days_ago=1):
    n = next(_n)
    contact = ContactFactory(
        name="Dana Reyes", email=f"dana{n}@example.com", phone=f"555123{n:04d}"
    )
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=contact)
    extra = {"service_type": ServiceTypeFactory(name=WEDDING_SERVICE_NAME)} if wedding else {}
    for i in range(trips):
        trip = ReservationFactory(
            lead=lead,
            pickup_date=timezone.localdate() - timedelta(days=days_ago),
            pickup_time=time(10 + i, 0),
            pickup_timezone="America/New_York",
            trip_status=status,
            **extra,
        )
        AssignmentFactory(reservation=trip, in_house=True)
    return lead


def _tp(lead, kind):
    return TouchPoint.objects.filter(lead=lead, kind=kind).first()


def _make_due(lead, kind):
    TouchPoint.objects.filter(lead=lead, kind=kind).update(
        scheduled_for=timezone.now() - timedelta(minutes=1)
    )


def _bodies(mock_send) -> dict[str, str]:
    return {c.kwargs["channel_type"]: c.kwargs["body"] for c in mock_send.call_args_list}


def _complain(lead, category=TripIssue.Category.COMPLAINT):
    review = reviews.review_for(lead.reservations.first())
    reviews.add_issue(
        review, category=category, severity=TripIssue.Severity.HIGH, note="x", user=UserFactory()
    )


# --- scheduling ------------------------------------------------------------------------


def test_a_finished_order_gets_one_thank_you_and_a_review_request_a_day_later():
    lead = _order()

    run_tasks()
    run_tasks()

    thanks = TouchPoint.objects.filter(lead=lead, kind=_K.THANK_YOU)
    asks = TouchPoint.objects.filter(lead=lead, kind=_K.REVIEW_REQUEST)
    assert thanks.count() == 1
    assert asks.count() == 1
    now = timezone.now()
    assert timedelta(hours=1, minutes=55) < thanks.get().scheduled_for - now <= timedelta(hours=2)
    assert timedelta(hours=23, minutes=55) < asks.get().scheduled_for - now <= timedelta(days=1)


def test_nothing_is_scheduled_while_a_trip_is_still_to_run():
    lead = _order()
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=2),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )

    run_tasks()

    assert not TouchPoint.objects.filter(lead=lead, kind__in=(_K.THANK_YOU, _K.REVIEW_REQUEST))


def test_mark_lost_does_not_leave_a_thank_you_behind_but_a_rebuild_spares_it():
    lead = _order()
    touchpoints.schedule_post_trip(lead)

    touchpoints.cancel_pending(lead)  # the quote-program rebuild

    assert _tp(lead, _K.THANK_YOU).status == TouchPoint.Status.SCHEDULED


# --- sending ---------------------------------------------------------------------------


def test_the_thank_you_goes_out_by_text_and_email_with_the_rating_link(live):
    lead = _order()
    touchpoints.schedule_post_trip(lead)
    _make_due(lead, _K.THANK_YOU)

    with patch(SEND, return_value={"uid": "m-1"}) as send:
        assert touchpoints.run_touchpoints() == 1

    assert _tp(lead, _K.THANK_YOU).status == TouchPoint.Status.SENT
    bodies = _bodies(send)
    day = lead.reservations.first().pickup_date
    link = "https://apc.example" + reverse(
        "trip_confirm", args=[ack.make_trip_day_ack_token(lead.contact, day)]
    )
    assert set(bodies) == {"phone", "email"}
    assert bodies["phone"].startswith("Dana, it's ")
    assert "Thank you for trusting us with your trip" in bodies["phone"]
    assert "Thank you for booking with us" in bodies["email"]
    assert all(link in body for body in bodies.values())


def test_a_wedding_gets_the_wedding_wording(live):
    lead = _order(wedding=True)
    touchpoints.schedule_post_trip(lead)
    _make_due(lead, _K.THANK_YOU)

    with patch(SEND, return_value={"uid": "m-1"}) as send:
        touchpoints.run_touchpoints()

    for body in _bodies(send).values():
        assert "Congratulations on the wedding" in body
        assert "letting us be part of the day" in body


def test_it_waits_when_touchpoints_are_off(settings):
    settings.TOUCHPOINTS_ENABLED = False
    lead = _order()
    touchpoints.schedule_post_trip(lead)
    _make_due(lead, _K.THANK_YOU)

    with patch(SEND) as send:
        assert touchpoints.run_touchpoints() == 0

    send.assert_not_called()
    assert _tp(lead, _K.THANK_YOU).status == TouchPoint.Status.SCHEDULED


def test_it_waits_for_a_public_base_url(live, settings):
    settings.PUBLIC_BASE_URL = ""
    lead = _order()
    touchpoints.schedule_post_trip(lead)
    _make_due(lead, _K.THANK_YOU)

    with patch(SEND) as send:
        touchpoints.run_touchpoints()

    send.assert_not_called()
    assert _tp(lead, _K.THANK_YOU).status == TouchPoint.Status.SCHEDULED


def test_customer_copy_does_not_read_like_a_bot():
    for kind in (_K.THANK_YOU, _K.REVIEW_REQUEST):
        template = TEMPLATES[kind]
        for text in (template.sms_body, template.email_body, template.subject):
            assert "!" not in text
            assert "—" not in text
    assert TEMPLATES[_K.REVIEW_REQUEST].sms_body.startswith("{first_name}, it's {company_name}.")


# --- the review hold (decision D3) -----------------------------------------------------


def test_a_complaint_holds_the_review_request_and_opens_a_follow_up(live):
    lead = _order()
    run_tasks()
    _complain(lead)
    _make_due(lead, _K.REVIEW_REQUEST)

    with patch(SEND) as send, patch(INVITE) as invite:
        touchpoints.run_touchpoints()

    invite.assert_not_called()
    send.assert_not_called()
    held = _tp(lead, _K.REVIEW_REQUEST)
    assert held.status == TouchPoint.Status.CANCELLED
    assert held.error == "Review request held: complaint logged"
    assert not Review.objects.exists()
    task = Task.objects.get(lead=lead, kind="complaint_followup")
    assert task.department == "customer_service"
    assert task.status == Task.Status.OPEN


def test_a_low_rating_holds_it_too(live):
    lead = _order()
    run_tasks()
    record_feedback(lead, rating=1, comment="Driver was late")
    _make_due(lead, _K.REVIEW_REQUEST)

    with patch(SEND), patch(INVITE) as invite:
        touchpoints.run_touchpoints()

    invite.assert_not_called()
    held = _tp(lead, _K.REVIEW_REQUEST)
    assert held.status == TouchPoint.Status.CANCELLED
    assert held.error == "Review request held: low rating"


def test_another_kind_of_issue_does_not_hold_it(live):
    lead = _order()
    run_tasks()
    _complain(lead, category=TripIssue.Category.VEHICLE)
    _make_due(lead, _K.REVIEW_REQUEST)

    with (
        patch(SEND, return_value={"uid": "m-1"}),
        patch(INVITE, return_value={"uid": "inv-1", "url": "https://podium.example/r/1"}),
    ):
        touchpoints.run_touchpoints()

    assert _tp(lead, _K.REVIEW_REQUEST).status == TouchPoint.Status.SENT
    assert not Task.objects.filter(lead=lead, kind="complaint_followup").exists()


# --- the "Thank-you sent" task ---------------------------------------------------------


def _reach_stage_3(lead):
    trip = lead.reservations.get()
    for kind in ("ops_review", "overtime_invoiced"):
        row = Task.objects.filter(reservation=trip, kind=kind).first()
        if row is not None and not row.is_closed:
            tasks.complete(row, UserFactory())
    return Task.objects.get(reservation=trip, kind="thank_you_sent")


def test_the_task_closes_itself_once_the_message_has_gone(live):
    lead = _order()
    run_tasks()
    _make_due(lead, _K.THANK_YOU)
    with patch(SEND, return_value={"uid": "m-1"}):
        touchpoints.run_touchpoints()

    task = _reach_stage_3(lead)

    assert task.status == Task.Status.DONE
    assert task.completed_by is None


def test_the_task_stays_open_for_a_person_when_nothing_was_sent():
    lead = _order()
    run_tasks()

    task = _reach_stage_3(lead)

    assert task.status == Task.Status.OPEN


# --- the trip page ---------------------------------------------------------------------


def _page(client, lead) -> str:
    day = lead.reservations.first().pickup_date
    token = ack.make_trip_day_ack_token(lead.contact, day)
    return client.get(reverse("trip_confirm", args=[token])).content.decode()


def test_the_heading_is_plural_for_more_than_one_trip(client):
    assert "How were your trips?" in _page(client, _order(trips=2))
    assert "How was your trip?" in _page(client, _order(trips=1))


def test_a_low_rating_is_answered_with_a_promise_we_keep(client):
    lead = _order()
    record_feedback(lead, rating=2, comment="")

    body = _page(client, lead)

    assert "Thank you for telling us. We'll be in touch about this." in body
    assert Task.objects.filter(lead=lead, kind="feedback_followup").exists()


def test_a_good_rating_keeps_the_plain_thanks(client):
    lead = _order()
    record_feedback(lead, rating=5, comment="")

    assert "Thanks for your feedback." in _page(client, lead)
