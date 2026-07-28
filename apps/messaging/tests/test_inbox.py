import json
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.leads.factories import LeadFactory
from apps.messaging.factories import ConversationFactory, MessageFactory
from apps.messaging.models import Conversation, Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


# --- list / ordering / search ---------------------------------------------------------


def test_inbox_requires_login(client):
    resp = client.get(reverse("inbox"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_inbox_shows_only_conversations_with_messages(client, agent):
    with_msg = ConversationFactory()
    MessageFactory(conversation=with_msg)
    ConversationFactory()  # no messages — should not appear

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    assert resp.status_code == 200
    assert b"Inbox" in resp.content
    conversations = list(resp.context["conversations"])
    assert conversations == [with_msg]


def test_inbox_orders_by_last_message_at_desc(client, agent):
    older = ConversationFactory()
    newer = ConversationFactory()
    MessageFactory(conversation=older)
    MessageFactory(conversation=newer)
    now = timezone.now()
    Conversation.objects.filter(pk=older.pk).update(created_at=now, last_message_at=now)
    Conversation.objects.filter(pk=newer.pk).update(
        created_at=now, last_message_at=now + timezone.timedelta(hours=1)
    )

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    conversations = list(resp.context["conversations"])
    assert conversations == [newer, older]


def test_inbox_hides_archived_conversations_by_default(client, agent):
    open_convo = ConversationFactory()
    archived = ConversationFactory(status=Conversation.Status.ARCHIVED)
    MessageFactory(conversation=open_convo)
    MessageFactory(conversation=archived)

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    assert list(resp.context["conversations"]) == [open_convo]


def test_archived_filter_shows_only_archived(client, agent):
    open_convo = ConversationFactory()
    archived = ConversationFactory(status=Conversation.Status.ARCHIVED)
    MessageFactory(conversation=open_convo)
    MessageFactory(conversation=archived)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"filter": "archived"})

    assert list(resp.context["conversations"]) == [archived]


def test_all_filter_shows_both(client, agent):
    open_convo = ConversationFactory()
    archived = ConversationFactory(status=Conversation.Status.ARCHIVED)
    MessageFactory(conversation=open_convo)
    MessageFactory(conversation=archived)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"filter": "all"})

    assert set(resp.context["conversations"]) == {open_convo, archived}


def test_unknown_filter_falls_back_to_open(client, agent):
    open_convo = ConversationFactory()
    archived = ConversationFactory(status=Conversation.Status.ARCHIVED)
    MessageFactory(conversation=open_convo)
    MessageFactory(conversation=archived)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"filter": "nonsense"})

    assert list(resp.context["conversations"]) == [open_convo]
    assert resp.context["conversation_filter"] == "open"


def test_thread_lists_the_contacts_leads(client, agent):
    contact = ContactFactory()
    convo = ConversationFactory(contact=contact)
    MessageFactory(conversation=convo)
    first = LeadFactory(contact=contact)
    second = LeadFactory(contact=contact)
    LeadFactory()  # another contact's lead — must not appear

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})

    assert set(resp.context["leads"]) == {first, second}


def test_thread_with_no_leads_shows_an_empty_rail(client, agent):
    convo = ConversationFactory()
    MessageFactory(conversation=convo)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})

    assert list(resp.context["leads"]) == []


def test_inbox_send_stamps_last_message_at(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="+16175551234"))
    MessageFactory(conversation=convo)
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message", return_value={"uid": "px1"}):
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "On our way", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    convo.refresh_from_db()
    assert convo.last_message_at is not None
    assert Message.objects.filter(conversation=convo, direction=Message.Direction.OUT).exists()


def test_inbox_unread_count_per_conversation(client, agent):
    convo = ConversationFactory()
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=timezone.now())
    MessageFactory(conversation=convo, direction=Message.Direction.OUT, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"))

    conversations = list(resp.context["conversations"])
    assert conversations[0].unread_count == 2


def test_inbox_search_matches_contact_name(client, agent):
    match = ConversationFactory(contact=ContactFactory(name="Wedding Wanda"))
    other = ConversationFactory(contact=ContactFactory(name="Someone Else"))
    MessageFactory(conversation=match)
    MessageFactory(conversation=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "wanda"})

    assert list(resp.context["conversations"]) == [match]


def test_inbox_search_matches_company(client, agent):
    match = ConversationFactory(
        contact=ContactFactory(name="Jane", company=CompanyFactory(name="Acme Events"))
    )
    other = ConversationFactory(
        contact=ContactFactory(name="John", company=CompanyFactory(name="Other Co"))
    )
    MessageFactory(conversation=match)
    MessageFactory(conversation=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "acme"})

    assert list(resp.context["conversations"]) == [match]


def test_inbox_search_matches_phone(client, agent):
    match = ConversationFactory(contact=ContactFactory(phone="555-123-9999"))
    other = ConversationFactory(contact=ContactFactory(phone="555-000-0000"))
    MessageFactory(conversation=match)
    MessageFactory(conversation=other)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"q": "9999"})

    assert list(resp.context["conversations"]) == [match]


# --- opening a thread marks read -------------------------------------------------------


def test_opening_thread_marks_inbound_messages_read(client, agent):
    convo = ConversationFactory()
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=convo, direction=Message.Direction.OUT, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})

    assert resp.status_code == 200
    assert resp.context["selected"] == convo
    assert len(resp.context["thread_messages"]) == 2

    # second render — unread should now be 0
    resp2 = client.get(reverse("inbox"))
    conversations = list(resp2.context["conversations"])
    assert conversations[0].unread_count == 0


def test_opening_thread_shows_cleared_unread_in_same_response(client, agent):
    """Opening a thread should mark messages read BEFORE building the conversation list,
    so the sidebar reflects the cleared unread count in the same response."""
    convo = ConversationFactory()
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})

    assert resp.status_code == 200
    # The selected thread's unread_count should be 0 in the same response
    conversations = list(resp.context["conversations"])
    assert len(conversations) == 1
    assert conversations[0].unread_count == 0


def test_opening_thread_for_conversation_without_messages_404s(client, agent):
    convo = ConversationFactory()
    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})
    assert resp.status_code == 404


def test_inbox_with_non_numeric_conversation_param_returns_404(client, agent):
    """Non-numeric conversation parameter should return 404, not 500."""
    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": "abc"})
    assert resp.status_code == 404


# --- send: happy paths -------------------------------------------------------------------


def test_inbox_send_sms_happy_path(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message", return_value={"uid": "msg-123"}) as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "On our way!", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True

    send.assert_called_once()
    assert send.call_args.kwargs["identifier"] == "555-123-4567"
    assert send.call_args.kwargs["channel_type"] == "phone"
    assert send.call_args.kwargs["body"] == "On our way!"

    message = Message.objects.get(conversation=convo)
    assert message.direction == Message.Direction.OUT
    assert message.channel == Message.Channel.SMS
    assert message.podium_message_uid == "msg-123"
    assert message.sent_at is not None
    assert message.delivery_status == Message.DeliveryStatus.SENT


def test_inbox_send_records_the_sending_user_as_sender(client, agent):
    """Composer sends know their author directly — no /users round-trip needed."""
    agent.first_name, agent.last_name = "Tarrick", "Ghannam"
    agent.save(update_fields=["first_name", "last_name"])
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message", return_value={"uid": "m-own"}):
        client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert Message.objects.get(conversation=convo).sender_name == "Tarrick Ghannam"


def test_inbox_send_falls_back_to_username_when_no_full_name(client, agent):
    agent.first_name, agent.last_name = "", ""
    agent.save(update_fields=["first_name", "last_name"])
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message", return_value={"uid": "m-own2"}):
        client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert Message.objects.get(conversation=convo).sender_name == agent.username


def test_inbox_send_email_happy_path(client, agent):
    convo = ConversationFactory(contact=ContactFactory(email="rider@example.com"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        return_value={"data": {"uid": "msg-456"}},
    ) as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Here is your quote", "channel": "email"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    send.assert_called_once()
    assert send.call_args.kwargs["identifier"] == "rider@example.com"
    assert send.call_args.kwargs["channel_type"] == "email"

    message = Message.objects.get(conversation=convo)
    assert message.channel == Message.Channel.EMAIL
    assert message.podium_message_uid == "msg-456"


def test_inbox_send_missing_phone_returns_400(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone=""))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(conversation=convo).exists()


def test_inbox_send_missing_channel_returns_400(client, agent):
    """Missing channel key should return 400, not silently default to SMS."""
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(conversation=convo).exists()


def test_inbox_send_null_channel_returns_400(client, agent):
    """An explicit null channel must 400, not crash on None.strip()."""
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi", "channel": None}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()


def test_inbox_send_invalid_channel_returns_400(client, agent):
    """Invalid channel value should return 400."""
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch("apps.messaging.views.podium.send_message") as send:
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi", "channel": "telegram"}),
            content_type="application/json",
        )

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    send.assert_not_called()
    assert not Message.objects.filter(conversation=convo).exists()


def test_inbox_send_podium_error_returns_502_with_message(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        side_effect=PodiumAPIError("502 boom"),
    ):
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 502
    payload = resp.json()
    assert payload["ok"] is False
    assert "boom" in payload["error"]
    assert not Message.objects.filter(conversation=convo).exists()


def test_inbox_send_podium_not_connected_returns_502_with_message(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="555-123-4567"))
    client.force_login(agent)

    with patch(
        "apps.messaging.views.podium.send_message",
        side_effect=PodiumNotConnected("no credential"),
    ):
        resp = client.post(
            reverse("inbox_send", args=[convo.pk]),
            data=json.dumps({"body": "Hi", "channel": "sms"}),
            content_type="application/json",
        )

    assert resp.status_code == 502
    payload = resp.json()
    assert payload["ok"] is False
    assert "no credential" in payload["error"]
    assert not Message.objects.filter(conversation=convo).exists()


def test_inbox_send_requires_login(client):
    convo = ConversationFactory()
    resp = client.post(
        reverse("inbox_send", args=[convo.pk]),
        data=json.dumps({"body": "Hi", "channel": "sms"}),
        content_type="application/json",
    )
    assert resp.status_code == 302
    assert "/login" in resp.url


# --- chrome context ---------------------------------------------------------------------


# --- rendering --------------------------------------------------------------------------


def test_inbox_page_renders_conversation_and_thread(client, agent):
    convo = ConversationFactory(contact=ContactFactory(name="Wedding Wanda"))
    MessageFactory(
        conversation=convo, direction=Message.Direction.IN, body="Hi there, are you free?"
    )
    MessageFactory(
        conversation=convo, direction=Message.Direction.OUT, body="Yes! Sending a quote."
    )

    client.force_login(agent)
    resp = client.get(reverse("inbox"), {"conversation": convo.pk})

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Inbox" in html
    assert "Wedding Wanda" in html
    assert "Hi there, are you free?" in html
    assert "Yes! Sending a quote." in html
    assert "<form" in html
    assert f'action="{reverse("inbox_send", args=[convo.pk])}"' in html


def test_inbox_page_shows_unread_badge(client, agent):
    convo = ConversationFactory(contact=ContactFactory(name="Unread Umberto"))
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)

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
    convo = ConversationFactory()
    MessageFactory(conversation=convo, direction=Message.Direction.IN, read_at=None)
    other_convo = ConversationFactory()
    MessageFactory(conversation=other_convo, direction=Message.Direction.IN, read_at=None)
    MessageFactory(conversation=other_convo, direction=Message.Direction.IN, read_at=None)

    client.force_login(agent)
    resp = client.get(reverse("dashboard"))

    assert resp.status_code == 200
    assert resp.context["inbox_unread"] == 2


# --- thread rendering: direction + sender attribution --------------------------------


def test_thread_renders_outbound_right_aligned_and_inbound_left(client, agent):
    """The bug that shipped: every message rendered left because all were stored IN."""
    convo = ConversationFactory(contact=ContactFactory(phone="+17035736008"))
    MessageFactory(conversation=convo, direction=Message.Direction.IN, body="Which hotel?")
    MessageFactory(conversation=convo, direction=Message.Direction.OUT, body="Fairview Marriott")
    client.force_login(agent)

    html = client.get(reverse("inbox"), {"conversation": convo.pk}).content.decode()

    assert "items-end" in html, "an outbound message must render right-aligned"
    assert "items-start" in html, "an inbound message must render left-aligned"


def test_thread_shows_sender_name_when_known(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="+17035736008"))
    MessageFactory(
        conversation=convo,
        direction=Message.Direction.OUT,
        body="On our way",
        sender_name="Tarrick Ghannam",
    )
    client.force_login(agent)

    html = client.get(reverse("inbox"), {"conversation": convo.pk}).content.decode()

    assert "Tarrick Ghannam" in html


def test_thread_falls_back_to_phone_when_sender_name_unknown(client, agent):
    convo = ConversationFactory(contact=ContactFactory(phone="+17035736008"))
    MessageFactory(conversation=convo, direction=Message.Direction.IN, body="hi", sender_name="")
    client.force_login(agent)

    html = client.get(reverse("inbox"), {"conversation": convo.pk}).content.decode()

    assert "(703) 573-6008" in html
