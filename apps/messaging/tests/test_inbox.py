import json
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads.factories import LeadFactory
from apps.messaging.factories import MessageFactory
from apps.messaging.models import Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


# --- list / ordering / search ---------------------------------------------------------


def test_inbox_requires_login(client):
    resp = client.get(reverse("inbox"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_inbox_shows_only_leads_with_messages(client, agent):
    with_msg = LeadFactory()
    MessageFactory(lead=with_msg)
    LeadFactory()  # no messages — should not appear

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    assert resp.status_code == 200
    assert b"Inbox" in resp.content
    conversations = list(resp.context["conversations"])
    assert conversations == [with_msg]


def test_inbox_orders_by_latest_message_desc(client, agent):
    older_lead = LeadFactory()
    newer_lead = LeadFactory()
    MessageFactory(lead=older_lead)
    MessageFactory(lead=newer_lead)
    # push older_lead's latest message further back in time
    Message.objects.filter(lead=older_lead).update(
        created_at=timezone.now() - timezone.timedelta(days=2)
    )

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    conversations = list(resp.context["conversations"])
    assert conversations == [newer_lead, older_lead]


def test_inbox_unread_count_per_conversation(client, agent):
    lead = LeadFactory()
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=timezone.now())
    MessageFactory(lead=lead, direction=Message.Direction.OUT, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    conversations = list(resp.context["conversations"])
    assert conversations[0].unread_count == 2


def test_inbox_search_matches_contact_name(client, agent):
    match = LeadFactory(contact=ContactFactory(name="Wedding Wanda"))
    other = LeadFactory(contact=ContactFactory(name="Someone Else"))
    MessageFactory(lead=match)
    MessageFactory(lead=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "wanda"})

    assert list(resp.context["conversations"]) == [match]


def test_inbox_search_matches_company(client, agent):
    match = LeadFactory(contact=ContactFactory(name="Jane", company="Acme Events"))
    other = LeadFactory(contact=ContactFactory(name="John", company="Other Co"))
    MessageFactory(lead=match)
    MessageFactory(lead=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "acme"})

    assert list(resp.context["conversations"]) == [match]


def test_inbox_search_matches_phone(client, agent):
    match = LeadFactory(contact=ContactFactory(phone="202-555-9999"))
    other = LeadFactory(contact=ContactFactory(phone="202-555-0100"))
    MessageFactory(lead=match)
    MessageFactory(lead=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "9999"})

    assert list(resp.context["conversations"]) == [match]


def test_inbox_search_alphanumeric_text_does_not_over_match_via_digit_collapse(client, agent):
    """ "Suite 5" must not collapse to digit "5" and match any contact whose number
    contains a 5 — it should only match on the name/company fields."""
    match = LeadFactory(contact=ContactFactory(name="Suite 5 Events", phone="202-555-0187"))
    other = LeadFactory(contact=ContactFactory(name="Random Corp", phone="202-555-0155"))
    MessageFactory(lead=match)
    MessageFactory(lead=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "Suite 5"})

    assert list(resp.context["conversations"]) == [match]


# --- opening a thread marks read -------------------------------------------------------


def test_opening_thread_marks_inbound_messages_read(client, agent):
    lead = LeadFactory()
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=lead, direction=Message.Direction.OUT, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"lead": lead.pk})

    assert resp.status_code == 200
    assert resp.context["selected"] == lead
    assert len(resp.context["thread_messages"]) == 2

    # second render — unread should now be 0
    resp2 = client.get(reverse("inbox"))
    conversations = list(resp2.context["conversations"])
    assert conversations[0].unread_count == 0


def test_opening_thread_shows_cleared_unread_in_same_response(client, agent):
    """Opening a thread should mark messages read BEFORE building the conversation list,
    so the sidebar reflects the cleared unread count in the same response."""
    lead = LeadFactory()
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"lead": lead.pk})

    assert resp.status_code == 200
    # The selected thread's unread_count should be 0 in the same response
    conversations = list(resp.context["conversations"])
    assert len(conversations) == 1
    assert conversations[0].unread_count == 0


def test_opening_thread_for_lead_without_messages_404s(client, agent):
    lead = LeadFactory()
    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"lead": lead.pk})
    assert resp.status_code == 404


def test_inbox_with_non_numeric_lead_param_returns_404(client, agent):
    """Non-numeric lead parameter should return 404, not 500."""
    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"lead": "abc"})
    assert resp.status_code == 404


# --- send: happy paths -------------------------------------------------------------------


def test_inbox_send_sms_happy_path(client, agent):
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message", return_value={"uid": "msg-123"}) as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "On our way!", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True

    send.assert_called_once()
    assert send.call_args.kwargs["identifier"] == "+12025550134"
    assert send.call_args.kwargs["channel_type"] == "phone"
    assert send.call_args.kwargs["body"] == "On our way!"

    message = Message.objects.get(lead=lead)
    assert message.direction == Message.Direction.OUT
    assert message.channel == Message.Channel.SMS
    assert message.podium_message_uid == "msg-123"
    assert message.sent_at is not None
    assert message.delivery_status == Message.DeliveryStatus.SENT


def test_inbox_send_email_happy_path(client, agent):
    lead = LeadFactory(contact=ContactFactory(email="rider@example.com"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        return_value={"data": {"uid": "msg-456"}},
    ) as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Here is your quote", "channel": "email"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    send.assert_called_once()
    assert send.call_args.kwargs["identifier"] == "rider@example.com"
    assert send.call_args.kwargs["channel_type"] == "email"

    message = Message.objects.get(lead=lead)
    assert message.channel == Message.Channel.EMAIL
    assert message.podium_message_uid == "msg-456"


def test_inbox_send_missing_phone_returns_400(client, agent):
    lead = LeadFactory(contact=ContactFactory(phone=""))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(lead=lead).exists()


def test_inbox_send_missing_channel_returns_400(client, agent):
    """Missing channel key should return 400, not silently default to SMS."""
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(lead=lead).exists()


def test_inbox_send_null_channel_returns_400(client, agent):
    """An explicit null channel must 400, not crash on None.strip()."""
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi", "channel": None}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()


def test_inbox_send_invalid_channel_returns_400(client, agent):
    """Invalid channel value should return 400."""
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi", "channel": "telegram"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(lead=lead).exists()


def test_inbox_send_podium_error_returns_502_with_message(client, agent):
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        side_effect=PodiumAPIError("502 boom"),
    ):
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 502
    payload = resp.json()
    assert payload["ok"] is False
    assert "boom" in payload["error"]
    assert not Message.objects.filter(lead=lead).exists()


def test_inbox_send_podium_not_connected_returns_502_with_message(client, agent):
    lead = LeadFactory(contact=ContactFactory(phone="202-555-0134"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        side_effect=PodiumNotConnected("no credential"),
    ):
        resp = client.post(
            reverse("inbox_send", args=[lead.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 502
    payload = resp.json()
    assert payload["ok"] is False
    assert "no credential" in payload["error"]
    assert not Message.objects.filter(lead=lead).exists()


def test_inbox_send_requires_login(client):
    lead = LeadFactory()
    resp = client.post(
        reverse("inbox_send", args=[lead.pk]),
        data=json.dumps({"body": "Hi", "channel": "sms"}),
        content_type="application/json",
    )
    assert resp.status_code == 302
    assert "/login" in resp.url


# --- chrome context ---------------------------------------------------------------------


# --- rendering --------------------------------------------------------------------------


def test_inbox_page_renders_conversation_and_thread(client, agent):
    lead = LeadFactory(contact=ContactFactory(name="Wedding Wanda"))
    MessageFactory(lead=lead, direction=Message.Direction.IN, body="Hi there, are you free?")
    MessageFactory(lead=lead, direction=Message.Direction.OUT, body="Yes! Sending a quote.")

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"lead": lead.pk})

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Inbox" in html
    assert "Wedding Wanda" in html
    assert "Hi there, are you free?" in html
    assert "Yes! Sending a quote." in html
    assert "<form" in html
    assert f'action="{reverse("inbox_send", args=[lead.pk])}"' in html


def test_inbox_page_shows_unread_badge(client, agent):
    lead = LeadFactory(contact=ContactFactory(name="Unread Umberto"))
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    html = resp.content.decode()
    assert "Unread Umberto" in html
    assert ">2<" in html


def test_inbox_nav_link_present_on_authed_page(client, agent):
    client.force_login(agent)
    resp = client.get(reverse("dashboard"))
    html = resp.content.decode()
    assert reverse("inbox") in html


def test_chrome_context_includes_inbox_unread_count(client, agent):
    lead = LeadFactory()
    MessageFactory(lead=lead, direction=Message.Direction.IN, read_at=None)
    other_lead = LeadFactory()
    MessageFactory(lead=other_lead, direction=Message.Direction.IN, read_at=None)
    MessageFactory(lead=other_lead, direction=Message.Direction.IN, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("dashboard"))

    assert resp.status_code == 200
    assert resp.context["inbox_unread"] == 2
