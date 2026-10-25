"""APC-68: "Request a change" from the planner dashboard → Inbox + Customer Service task."""

import uuid
from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging.models import Conversation, Message
from apps.planners import services
from apps.planners.models import PlannerRequest
from apps.reservations.models import Reservation, Stop
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-pass-phrase-77"


@pytest.fixture
def planner(client):
    contact = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    services.register(account, PASSWORD)
    client.post(reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD})
    return account


def _wedding(account, *, days=30, status=Lead.Status.BOOKED, role=True):
    lead = LeadFactory(status=status, wedding_name="Whitfield & Okafor")
    key = uuid.uuid4()
    for _ in range(2):
        trip = Reservation.objects.create(
            lead=lead,
            service_type=ServiceTypeFactory(name="Hotel departures"),
            pickup_date=timezone.localdate() + timedelta(days=days),
            pickup_time=time(14, 30),
            pickup_timezone="America/New_York",
            group_key=key,
        )
        Stop.objects.create(reservation=trip, sequence=0, name="The Line DC")
        Stop.objects.create(reservation=trip, sequence=1, name="Cathedral")
    if role:
        LeadContact.objects.create(
            lead=lead, contact=account.contact, role=LeadContact.Role.LEAD_PLANNER
        )
    return lead, key


def _url(lead):
    return reverse("planners:request_change", args=[lead.pk])


def test_a_submission_creates_one_inbound_message_and_one_task(client, planner):
    lead, key = _wedding(planner)
    response = client.post(
        _url(lead), {"about": f"run:{key}", "body": "Ceremony moved to 3:30, move pickups up."}
    )
    assert response.status_code == 302
    assert response.url == f"{reverse('planners:wedding', args=[lead.pk])}?change=sent#request"

    message = Message.objects.get()
    assert message.direction == Message.Direction.IN
    assert message.channel == Message.Channel.PORTAL
    assert message.conversation.contact == planner.contact
    assert "Ceremony moved to 3:30" in message.body
    assert "Whitfield & Okafor" in message.body
    assert "Hotel departures" in message.body
    assert message.sender_name == "Dana Planner"

    task = Task.objects.get(kind="planner_change_request")
    assert task.lead == lead
    assert task.department == "customer_service"
    assert task.status == Task.Status.OPEN

    req = PlannerRequest.objects.get()
    assert (req.account, req.lead, req.message, req.task) == (planner, lead, message, task)


def test_the_conversation_shows_up_in_the_inbox_with_the_marker(client, planner):
    lead, _ = _wedding(planner)
    client.post(_url(lead), {"about": "", "body": "Add a late shuttle at midnight."})
    conversation = Conversation.objects.get(contact=planner.contact)
    staff = client.__class__()
    staff.force_login(UserFactory())
    html = staff.get(f"{reverse('inbox')}?conversation={conversation.pk}").content.decode()
    assert "Planner request" in html
    assert "Add a late shuttle at midnight." in html
    assert reverse("lead_detail", args=[lead.pk]) in html


def test_the_task_queue_links_to_the_request(client, planner):
    lead, _ = _wedding(planner)
    client.post(_url(lead), {"about": "", "body": "Add a late shuttle."})
    staff = client.__class__()
    staff.force_login(UserFactory())
    html = staff.get(f"{reverse('task_queue')}?assignee=anyone").content.decode()
    conversation = Conversation.objects.get(contact=planner.contact)
    assert "Planner change request" in html
    assert f"{reverse('inbox')}?conversation={conversation.pk}" in html


def test_a_blank_request_is_refused(client, planner):
    lead, _ = _wedding(planner)
    response = client.post(_url(lead), {"about": "", "body": "   "})
    assert response.status_code == 302
    assert "change=form" in response.url
    assert not Message.objects.exists() and not Task.objects.exists()


def test_the_rate_limit_applies(client, planner):
    lead, _ = _wedding(planner)
    for i in range(services.REQUEST_LIMIT):
        client.post(_url(lead), {"about": "", "body": f"request {i}"})
    response = client.post(_url(lead), {"about": "", "body": "one too many"})
    assert response.url.endswith("?change=limited#request")
    assert PlannerRequest.objects.count() == services.REQUEST_LIMIT
    assert not Message.objects.filter(body__contains="one too many").exists()


def test_the_limit_is_a_rolling_hour(client, planner):
    lead, _ = _wedding(planner)
    for i in range(services.REQUEST_LIMIT):
        client.post(_url(lead), {"about": "", "body": f"request {i}"})
    PlannerRequest.objects.update(created_at=timezone.now() - timedelta(minutes=61))
    response = client.post(_url(lead), {"about": "", "body": "an hour later"})
    assert response.url.endswith("?change=sent#request")


def test_signed_out_is_refused(client):
    lead = LeadFactory()
    response = client.post(_url(lead), {"body": "hi"})
    assert response.status_code == 302 and response.url.startswith(reverse("planners:login"))
    assert not Message.objects.exists()


def test_a_wedding_without_a_role_is_refused(client, planner):
    lead, _ = _wedding(planner, role=False)
    assert client.post(_url(lead), {"body": "hi"}).status_code == 404
    assert not Message.objects.exists() and not Task.objects.exists()


def test_an_archived_wedding_is_refused(client, planner):
    lead, _ = _wedding(planner, days=-10)
    Reservation.objects.filter(lead=lead).update(trip_status=Reservation.TripStatus.DONE)
    assert client.post(_url(lead), {"body": "hi"}).status_code == 403
    assert not Message.objects.exists() and not Task.objects.exists()


def test_get_is_not_allowed(client, planner):
    lead, _ = _wedding(planner)
    assert client.get(_url(lead)).status_code == 405


def test_csrf_is_required(planner):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.post(reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD})
    lead, _ = _wedding(planner)
    assert strict.post(_url(lead), {"body": "hi"}).status_code == 403
    assert not Message.objects.exists()


def test_an_archived_conversation_still_records_and_opens_the_task(client, planner):
    Conversation.objects.create(contact=planner.contact, status=Conversation.Status.ARCHIVED)
    lead, _ = _wedding(planner)
    client.post(_url(lead), {"about": "", "body": "Still need a change."})
    conversation = Conversation.objects.get(contact=planner.contact)
    assert conversation.status == Conversation.Status.ARCHIVED  # archive never reopens
    assert conversation.messages.count() == 1
    assert Task.objects.filter(kind="planner_change_request", lead=lead).count() == 1


# --- the form on the dashboard ----------------------------------------------------------


def _page(client, lead, query=""):
    return client.get(reverse("planners:wedding", args=[lead.pk]) + query).content.decode()


def test_the_dashboard_offers_the_form_with_runs_and_trips_to_pick(client, planner):
    lead, key = _wedding(planner)
    html = _page(client, lead)
    assert 'id="request"' in html
    assert _url(lead) in html
    assert f'value="run:{key}"' in html
    trip = lead.reservations.first()
    assert f'value="trip:{trip.pk}"' in html
    assert "<select" in html and "data-tom" in html  # the searchable select, not a bare one


def test_a_run_link_preselects_that_run(client, planner):
    lead, key = _wedding(planner)
    html = _page(client, lead, f"?about=run:{key}#request")
    assert f'value="run:{key}" selected' in html


def test_sent_and_limited_states(client, planner):
    lead, _ = _wedding(planner)
    assert "Thanks, we'll be in touch." in _page(client, lead, "?change=sent")
    limited = _page(client, lead, "?change=limited")
    assert "That's a lot of requests" in limited
    assert "tel:+12024242600" in limited


def test_an_archived_wedding_has_no_form(client, planner):
    lead, _ = _wedding(planner, days=-10)
    Reservation.objects.filter(lead=lead).update(trip_status=Reservation.TripStatus.DONE)
    html = _page(client, lead)
    assert 'id="request"' not in html
    assert _url(lead) not in html


def test_a_request_over_the_limit_is_refused_not_cut(client, planner):
    lead, _ = _wedding(planner)
    response = client.post(
        _url(lead), {"about": "", "body": "x" * (services.MAX_REQUEST_LENGTH + 1)}, follow=True
    )
    assert "Please keep it under 2,000 characters." in response.content.decode()
    assert not Message.objects.exists() and not Task.objects.exists()


def test_a_double_submit_is_one_request(client, planner):
    lead, _ = _wedding(planner)
    data = {"about": "", "body": "Add a late shuttle at midnight."}
    first = client.post(_url(lead), data)
    second = client.post(_url(lead), data)
    assert first.url == second.url and second.url.endswith("?change=sent#request")
    assert Message.objects.count() == 1 and Task.objects.count() == 1
    assert PlannerRequest.objects.count() == 1


def test_the_same_words_later_or_elsewhere_are_a_new_request(client, planner):
    lead, _ = _wedding(planner)
    other, _ = _wedding(planner)
    data = {"about": "", "body": "Add a late shuttle at midnight."}
    client.post(_url(lead), data)
    client.post(_url(other), data)
    PlannerRequest.objects.update(created_at=timezone.now() - timedelta(seconds=31))
    client.post(_url(lead), data)
    assert PlannerRequest.objects.count() == 3
