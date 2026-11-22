"""Asking for a change or a cancellation (spec §9, mockups 6 and 7): each request is an
inbound message in the staff Inbox and a Customer Service task. The trip never changes."""

from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.customers import requests
from apps.customers.factories import CustomerAccountFactory
from apps.customers.models import CustomerAccount, CustomerRequest
from apps.customers.requests import RequestRefused
from apps.customers.selectors import trip_or_404
from apps.customers.trip_detail import trip_detail
from apps.leads import services as lead_services
from apps.leads.models import Lead
from apps.messaging import services as messaging
from apps.messaging.models import Conversation, Message
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status
from apps.tasks import services as task_services
from apps.tasks.definitions import KINDS, AfterOpenBusinessDays, Level
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
NOW = "django.utils.timezone.now"
NY = "America/New_York"
LA = "America/Los_Angeles"
AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _day(days: int):
    return timezone.localdate() + timedelta(days=days)


@pytest.fixture
def booked(account: CustomerAccount, make_trip: Callable[..., Reservation]):
    """`booked(days=30, **kw)` -> a trip on the account booked through `book_lead`, DCA to
    an office, picking up at 9:10 AM in New York."""

    def _booked(days: int = 30, **kw: object) -> Reservation:
        kw.setdefault("pickup_time", time(9, 10))
        kw.setdefault("pickup_timezone", NY)
        kw.setdefault("stops", ["Reagan National Airport (DCA)", "1100 Pennsylvania Ave NW"])
        trip = make_trip(account, lead__status=Lead.Status.QUOTED, pickup_date=_day(days), **kw)
        lead_services.book_lead(trip.lead)
        trip.refresh_from_db()
        return trip

    return _booked


def _ask(account, trip, kind="change", topics=("Date or time",), reason="", body="Flight moved"):
    return requests.ask(
        account,
        trip_or_404(account, trip.pk),
        kind=kind,
        topics=list(topics),
        reason=reason,
        body=body,
    )


def _detail(account, trip) -> dict:
    return trip_detail(account, trip_or_404(account, trip.pk))


def _when(trip: Reservation) -> str:
    day = trip.pickup_date
    return f"{day:%a, %b} {day.day} at 9:10 AM {trip.pickup_tz_label}"


# --- the request ------------------------------------------------------------------------------


def test_change_request_records_inbox_message_and_task(account, booked):
    trip = booked()

    req = _ask(account, trip, topics=["Date or time", "Flight"], body="  Flight moved  ")

    assert req.kind == "change" and req.reservation == trip and req.account == account
    assert req.topics == "Date or time, Flight" and req.reason == ""
    assert req.message.channel == "portal" and req.task.kind == "customer_change_request"
    message = req.message
    assert message.direction == Message.Direction.IN
    assert message.conversation.contact == account.contact
    assert message.sender_name == account.contact.name
    assert message.body == (
        f"Customer change request · {trip.reference} · {_when(trip)}\n"
        "About: Date or time, Flight\n\nFlight moved"
    )
    task = req.task
    assert task.lead == trip.lead and task.status == Task.Status.OPEN
    assert task.department == "customer_service"


def test_cancel_request_does_not_cancel_the_trip(account, booked):
    trip = booked()
    before = trip.trip_status

    req = _ask(account, trip, kind="cancel", topics=[], reason="Plans changed", body="")

    trip.refresh_from_db()
    assert trip.trip_status == before and trip.lead.status == "booked"
    assert trip.cancelled_at is None
    assert req.task.kind == "customer_cancel_request"
    assert req.reason == "Plans changed" and req.topics == ""
    assert req.message.body == (
        f"Customer cancellation request · {trip.reference} · {_when(trip)}\nReason: Plans changed"
    )


def test_a_cancel_note_follows_the_reason(account, booked):
    req = _ask(account, booked(), kind="cancel", reason="Booked by mistake", body="Wrong day")

    assert req.message.body.endswith("\nReason: Booked by mistake\n\nWrong day")


def test_only_listed_topics_and_reasons_are_kept(account, booked):
    trip = booked()

    change = _ask(account, trip, topics=["Vehicle", "<b>Free text</b>", "Vehicle"], body="Bigger")
    cancel = _ask(account, trip, kind="cancel", reason="I just felt like it", body="")
    no_topic = _ask(account, trip, topics=[], body="Something")

    assert change.topics == "Vehicle"
    assert cancel.reason == "Other" and "\nReason: Other" in cancel.message.body
    assert no_topic.topics == "" and "About:" not in no_topic.message.body


def test_recorded_on_the_accounts_own_conversation_when_billed_for_anothers_order(
    account, make_trip
):
    booker = ContactFactory(name="Office Manager")
    trip = make_trip(
        CustomerAccountFactory(contact=booker),
        lead__status=Lead.Status.QUOTED,
        pickup_date=_day(20),
        pickup_time=time(9, 10),
    )
    lead_services.book_lead(trip.lead)
    trip.lead.billing_contact = account.contact
    trip.lead.save(update_fields=["billing_contact"])

    req = _ask(account, trip)

    assert req.message.conversation.contact == account.contact
    assert req.task.lead == trip.lead
    assert not Conversation.objects.filter(contact=booker).exists()


def test_archived_conversation_still_records_and_opens_task(account, booked):
    conversation = messaging.conversation_for(account.contact)
    conversation.status = Conversation.Status.ARCHIVED
    conversation.save(update_fields=["status"])

    req = _ask(account, booked())

    conversation.refresh_from_db()
    assert req.message.conversation == conversation
    assert conversation.status == Conversation.Status.ARCHIVED
    assert req.task.status == Task.Status.OPEN


# --- refusals ---------------------------------------------------------------------------------


def test_blank_change_body_is_refused(account, booked):
    trip = booked()

    with pytest.raises(RequestRefused) as refused:
        _ask(account, trip, body="   ")

    assert refused.value.reason == "blank"
    assert not CustomerRequest.objects.exists() and not Message.objects.exists()
    assert not Task.objects.filter(kind="customer_change_request").exists()


def test_body_over_2000_characters_is_refused(account, booked):
    trip = booked()

    with pytest.raises(RequestRefused) as refused:
        _ask(account, trip, body="x" * 2001)
    assert refused.value.reason == "too_long"
    with pytest.raises(RequestRefused):
        _ask(account, trip, kind="cancel", reason="Other", body="x" * 2001)

    assert _ask(account, trip, body="x" * 2000).pk


def test_request_refused_after_pickup_time(account, booked):
    trip = booked(days=1)
    after = trip.pickup_at + timedelta(minutes=1)

    with patch(NOW, return_value=after), pytest.raises(RequestRefused) as refused:
        _ask(account, trip, kind="cancel", reason="Plans changed", body="")

    assert refused.value.reason == "closed"
    with patch(NOW, return_value=trip.pickup_at - timedelta(minutes=1)):
        assert _ask(account, trip).pk


@pytest.mark.parametrize("status", [S.ON_THE_WAY, S.DONE, S.CANCELLED])
def test_a_trip_under_way_done_or_cancelled_is_closed(account, booked, status):
    trip = booked(days=0, pickup_time=time(23, 59))
    set_trip_status(trip, status)

    with pytest.raises(RequestRefused) as refused:
        _ask(account, trip)

    assert refused.value.reason == "closed"


def test_an_unpriced_request_is_closed(account, make_trip):
    trip = make_trip(account, lead__status=Lead.Status.NEW, pickup_date=_day(20))

    with pytest.raises(RequestRefused) as refused:
        _ask(account, trip)

    assert refused.value.reason == "closed"


def test_double_tap_creates_one_request(account, booked):
    trip = booked()

    first = _ask(account, trip)
    second = _ask(account, trip)

    assert first.pk == second.pk
    assert CustomerRequest.objects.count() == 1 and Message.objects.count() == 1
    assert Task.objects.filter(kind="customer_change_request").count() == 1
    with patch(NOW, return_value=timezone.now() + timedelta(seconds=31)):
        assert _ask(account, trip).pk != first.pk


def test_eleventh_request_in_an_hour_is_limited(account, booked):
    trip = booked()
    for i in range(requests.ASK_LIMIT):
        _ask(account, trip, body=f"Change number {i}")

    with pytest.raises(RequestRefused) as refused:
        _ask(account, trip, body="One more")
    assert refused.value.reason == "limited"

    with patch(NOW, return_value=timezone.now() + timedelta(hours=1, seconds=1)):
        assert _ask(account, trip, body="One more").pk


# --- the views --------------------------------------------------------------------------------


def _url(name: str, trip: Reservation) -> str:
    return reverse(f"customers:{name}", args=[trip.pk])


def test_request_on_other_customers_trip_is_404(account, client_as, booked, make_trip):
    theirs = make_trip(CustomerAccountFactory(), pickup_date=_day(20))
    client = client_as(account)

    for name in ("ask_change", "ask_cancel"):
        response = client.post(_url(name, theirs), {"body": "x", "reason": "Other"}, **AJAX)
        assert response.status_code == 404
    assert not CustomerRequest.objects.exists() and not Message.objects.exists()


def test_posting_a_change_answers_ok_and_the_trip_page_toasts(account, client_as, booked):
    trip = booked()
    client = client_as(account)

    response = client.post(
        _url("ask_change", trip),
        {"topics": ["Pickup", "Passengers"], "body": "Two more riders, pick up at the hotel"},
        **AJAX,
    )

    assert response.status_code == 200 and response.json() == {"ok": True}
    req = CustomerRequest.objects.get()
    assert req.kind == "change" and req.topics == "Pickup, Passengers"
    assert "Request sent." in client.get(_url("trip", trip)).content.decode()


def test_posting_a_cancel_answers_ok(account, client_as, booked):
    trip = booked()

    response = client_as(account).post(
        _url("ask_cancel", trip), {"reason": "Found another option", "body": ""}, **AJAX
    )

    assert response.json() == {"ok": True}
    assert CustomerRequest.objects.get().kind == "cancel"


@pytest.mark.parametrize(
    ("data", "words"),
    [
        ({"body": " "}, "Tell us what needs to change"),
        ({"body": "x" * 2001}, "2,000 characters"),
    ],
)
def test_a_refused_change_says_why(account, client_as, booked, data, words):
    response = client_as(account).post(_url("ask_change", booked()), data, **AJAX)

    assert response.status_code == 400
    assert response.json()["ok"] is False and words in response.json()["error"]


def test_a_limited_or_closed_request_points_to_dispatch(account, client_as, booked):
    trip = booked()
    client = client_as(account)
    for i in range(requests.ASK_LIMIT):
        _ask(account, trip, body=f"Change {i}")

    limited = client.post(_url("ask_change", trip), {"body": "again"}, **AJAX).json()
    set_trip_status(trip, S.DONE)
    closed = client.post(_url("ask_cancel", trip), {"reason": "Other"}, **AJAX).json()

    assert "in the last hour" in limited["error"] and "call dispatch" in limited["error"]
    assert "online" in closed["error"] and "call dispatch" in closed["error"]


def test_signed_out_post_answers_401_and_get_is_refused(account, client_as, booked):
    trip = booked()

    assert Client().post(_url("ask_change", trip), {"body": "x"}, **AJAX).status_code == 401
    assert client_as(account).get(_url("ask_change", trip)).status_code == 405
    assert not CustomerRequest.objects.exists()


# --- pending ----------------------------------------------------------------------------------


def test_trip_shows_pending_until_task_closed(account, client_as, booked):
    trip = booked()
    client = client_as(account)
    req = _ask(account, trip)
    day = timezone.localtime(req.created_at, ZoneInfo(NY)).date()
    date = f"{day:%b} {day.day}"
    label = f"Change requested {date}"

    assert _detail(account, trip)["pending_request"] == {"kind": "change", "date": date}
    body = client.get(_url("trip", trip)).content.decode()
    assert label in body
    assert 'id="ask-change"' not in body and 'id="ask-cancel"' in body

    _close(req.task)
    assert _detail(account, trip)["pending_request"] is None
    body = client.get(_url("trip", trip)).content.decode()
    assert label not in body and 'id="ask-change"' in body


def _close(task: Task) -> None:
    task.status = Task.Status.DONE
    task.save(update_fields=["status"])


def test_a_pending_change_does_not_block_a_cancel(account, client_as, booked):
    trip = booked()
    _ask(account, trip)

    cancel = _ask(account, trip, kind="cancel", reason="Plans changed", body="")

    assert cancel.pk
    detail = _detail(account, trip)
    assert detail["pending_request"]["kind"] == "cancel"
    body = client_as(account).get(_url("trip", trip)).content.decode()
    assert "Change requested" in body and "Cancellation requested" in body


def test_the_pending_date_is_the_trips_own_day(account, booked):
    trip = booked(pickup_timezone=LA)
    # 3:30 AM on Nov 21 in UTC is still Nov 20 in Los Angeles.
    with patch(NOW, return_value=datetime(2026, 11, 21, 3, 30, tzinfo=UTC)):
        trip.pickup_date = datetime(2026, 12, 3).date()
        trip.save(update_fields=["pickup_date"])
        _ask(account, trip)
        assert _detail(account, trip)["pending_request"]["date"] == "Nov 20"


def test_another_accounts_request_is_not_shown_as_pending(account, client_as, make_trip):
    booker = CustomerAccountFactory()
    trip = make_trip(booker, lead__status=Lead.Status.QUOTED, pickup_date=_day(20))
    lead_services.book_lead(trip.lead)
    trip.lead.billing_contact = account.contact
    trip.lead.save(update_fields=["billing_contact"])
    _ask(booker, trip)

    assert _detail(account, trip)["pending_request"] is None


# --- the trip page and Home -------------------------------------------------------------------


def test_trip_page_offers_both_dialogs_in_the_mockups_words(account, client_as, booked):
    trip = booked(days=12)

    body = client_as(account).get(_url("trip", trip)).content.decode()

    for text in (
        "Request a change",
        "Request to cancel",
        "What needs to change?",
        *requests.CHANGE_TOPICS,
        "Your trip stays as booked until we confirm the change. If it affects the price, "
        "we'll send an updated quote.",
        "Keep trip as is",
        "Send request",
        "Anything else we should know",
        *requests.CANCEL_REASONS,
        "Your trip stays booked until we confirm the cancellation by email.",
        "Keep my trip",
        "Send cancellation request",
        f"{_when(trip)} · Reagan National Airport (DCA) to 1100 Pennsylvania Ave NW",
        "Your pickup is 12 days away.",
        _url("ask_change", trip),
        _url("ask_cancel", trip),
    ):
        assert text in body, text
    assert "full refund" not in body


def test_the_cancel_dialog_shows_the_real_cancellation_policy(account, client_as, booked):
    body = client_as(account).get(_url("trip", booked())).content.decode()

    for line in (
        "Sedans/SUVs must be cancelled 72 hours prior to service date.",
        "Luxury Vans must be cancelled 14 days prior to service date.",
        "Buses, Stretch Limos, and Speciality Vehicles must be cancelled 30 days prior to the\n"
        "service.",
    ):
        assert line in body


def test_the_public_terms_render_the_same_cancellation_policy():
    text = render_to_string("public/_terms.html")

    assert (
        "<h3>Cancellation Policy</h3>\n"
        "<p>Sedans/SUVs must be cancelled 72 hours prior to service date.</p>\n"
        "<p>Luxury Vans must be cancelled 14 days prior to service date.</p>\n"
        "<p>Buses, Stretch Limos, and Speciality Vehicles must be cancelled 30 days prior to the\n"
        "service.</p>\n\n<h3>Liability</h3>"
    ) in text


@pytest.mark.parametrize(
    ("left", "text"),
    [
        (timedelta(hours=30), "Your pickup is 30 hours away."),
        (timedelta(hours=1, minutes=5), "Your pickup is 1 hour away."),
        (timedelta(minutes=20), "Your pickup is less than an hour away."),
    ],
)
def test_time_until_pickup_counts_hours_under_two_days(account, booked, left, text):
    trip = booked(days=12)
    with patch(NOW, return_value=trip.pickup_at - left):
        assert _detail(account, trip)["pickup_in"] == text


def test_time_until_pickup_counts_days_in_the_trips_zone(account, booked):
    trip = booked(pickup_timezone=LA)
    trip.pickup_date = datetime(2026, 12, 3).date()
    trip.save(update_fields=["pickup_date"])
    # 2 AM UTC on Nov 21 is the evening of Nov 20 in Los Angeles: 13 days before Dec 3.
    with patch(NOW, return_value=datetime(2026, 11, 21, 2, 0, tzinfo=UTC)):
        assert _detail(account, trip)["pickup_in"] == "Your pickup is 13 days away."


def test_no_buttons_once_the_trip_has_started(account, client_as, booked):
    trip = booked(days=0, pickup_time=time(23, 59))
    set_trip_status(trip, S.ON_THE_WAY)

    body = client_as(account).get(_url("trip", trip)).content.decode()

    assert "Request a change" not in body and "Request to cancel" not in body


def test_home_next_trip_links_to_the_change_dialog(account, client_as, booked):
    trip = booked()
    client = client_as(account)

    link = f'href="{_url("trip", trip)}?ask=change"'
    assert link in client.get(reverse("customers:home")).content.decode()
    _ask(account, trip)
    assert link not in client.get(reverse("customers:home")).content.decode()


# --- staff side -------------------------------------------------------------------------------


def _inbox(conversation: Conversation) -> str:
    client = Client()
    client.force_login(UserFactory())
    url = f"{reverse('inbox')}?conversation={conversation.pk}"
    return client.get(url).content.decode()


def test_the_inbox_shows_the_request_escaped_as_a_customers(account, booked, two_factor_off):
    trip = booked()
    req = _ask(account, trip, body="<script>alert(1)</script>")

    html = _inbox(req.message.conversation)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Customer request" in html and "Planner request" not in html
    assert reverse("lead_detail", args=[trip.lead_id]) in html
    # A customer's thread is still a customer's: Create lead stays.
    conversation = req.message.conversation
    assert reverse("conversation_create_lead", args=[conversation.pk]) in html


def test_planner_requests_still_work_after_channel_relabel(db):
    from apps.leads.factories import LeadFactory
    from apps.planners import services as planner_services
    from apps.planners.models import PlannerAccount

    assert Message.Channel.PORTAL == "portal" and Message.Channel.PORTAL.label == "Portal"
    contact = ContactFactory()
    planner = PlannerAccount.objects.create(contact=contact, email="p@example.com")
    lead = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Whitfield")

    req = planner_services.request_change(planner, lead, about="", body="Move it")

    assert req.message.channel == Message.Channel.PORTAL
    assert req.task.kind == "planner_change_request"


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("customer_change_request", "Customer change request"),
        ("customer_cancel_request", "Customer cancellation request"),
    ],
)
def test_the_task_kinds(key, label, make_trip, account):
    kind = KINDS[key]
    assert kind.label == label
    assert kind.department == "customer_service" and kind.level == Level.ORDER
    assert kind.due == AfterOpenBusinessDays(days=1)

    trip = make_trip(account, lead__status=Lead.Status.BOOKED, pickup_date=_day(20))
    task_services.ensure_tasks(trip.lead)
    assert not Task.objects.filter(kind=key).exists()
