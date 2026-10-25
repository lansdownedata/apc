"""Review fix: a planner's conversation in the staff Inbox (APC-68)."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging import services as messaging
from apps.messaging.models import Message
from apps.planners import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _thread(staff, conversation) -> str:
    return staff.get(f"{reverse('inbox')}?conversation={conversation.pk}").content.decode()


def test_a_planner_thread_shows_their_weddings_and_no_create_lead(staff):
    planner = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        services.invite(planner, actor=UserFactory())
    wedding = LeadFactory(status=Lead.Status.BOOKED, wedding_name="Whitfield & Okafor")
    LeadContact.objects.create(lead=wedding, contact=planner, role=LeadContact.Role.LEAD_PLANNER)
    conversation = messaging.conversation_for(planner)
    messaging.record_inbound(conversation, channel=Message.Channel.SMS, body="hello")

    html = _thread(staff, conversation)
    assert reverse("conversation_create_lead", args=[conversation.pk]) not in html
    assert reverse("lead_detail", args=[wedding.pk]) in html
    assert "Not qualified yet" not in html


def test_a_portal_message_marks_the_thread_as_a_planners_even_without_an_account(staff):
    contact = ContactFactory()
    conversation = messaging.conversation_for(contact)
    messaging.record_inbound(conversation, channel=Message.Channel.PORTAL, body="change please")
    html = _thread(staff, conversation)
    assert reverse("conversation_create_lead", args=[conversation.pk]) not in html


def test_an_ordinary_customer_thread_is_unchanged(staff):
    contact = ContactFactory()
    own = LeadFactory(contact=contact)
    conversation = messaging.conversation_for(contact)
    messaging.record_inbound(conversation, channel=Message.Channel.SMS, body="quote?")
    html = _thread(staff, conversation)
    assert reverse("conversation_create_lead", args=[conversation.pk]) in html
    assert reverse("lead_detail", args=[own.pk]) in html


def test_script_in_a_request_body_renders_escaped(staff, client):
    contact = ContactFactory()
    conversation = messaging.conversation_for(contact)
    messaging.record_inbound(
        conversation, channel=Message.Channel.PORTAL, body="<script>alert(1)</script>"
    )
    html = _thread(staff, conversation)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
