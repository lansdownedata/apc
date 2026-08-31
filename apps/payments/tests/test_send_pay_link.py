"""The "Send payment link" agent action (spec 2026-08-30 §9).

Sends the customer pay-page link over Podium (SMS preferred, email fallback) and records it
as an outbound message on the conversation, so the office can nudge a no-card order without
waiting for the automated reminder.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import Conversation, Message
from apps.payments.factories import PaymentPlanFactory

pytestmark = pytest.mark.django_db


def _setup(**contact_kwargs):
    contact = ContactFactory(**contact_kwargs)
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=contact)
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    return lead


def _owner_client(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    return client


def test_sends_over_sms_when_a_phone_exists(client):
    lead = _setup(phone="+15715551212", email="rider@example.com")
    _owner_client(client)
    with patch("apps.payments.views.podium.send_message", return_value={"uid": "m1"}) as send:
        resp = client.post(reverse("order_send_pay_link", args=[lead.pk]))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "channel": "sms"}
    assert send.call_args.kwargs["channel_type"] == "phone"
    assert "/quote/" in send.call_args.kwargs["body"]


def test_falls_back_to_email_when_there_is_no_phone(client):
    lead = _setup(phone="", email="rider@example.com")
    _owner_client(client)
    with patch("apps.payments.views.podium.send_message", return_value={"uid": "m2"}) as send:
        resp = client.post(reverse("order_send_pay_link", args=[lead.pk]))
    assert resp.json() == {"ok": True, "channel": "email"}
    assert send.call_args.kwargs["channel_type"] == "email"


def test_records_an_outbound_message_on_the_conversation(client):
    lead = _setup(phone="+15715551212", email="rider@example.com")
    _owner_client(client)
    with patch("apps.payments.views.podium.send_message", return_value={"uid": "m3"}):
        client.post(reverse("order_send_pay_link", args=[lead.pk]))
    convo = Conversation.objects.get(contact=lead.contact)
    msg = convo.messages.get()
    assert msg.direction == Message.Direction.OUT
    assert msg.channel == Message.Channel.SMS
    assert "/quote/" in msg.body


def test_refuses_when_the_contact_has_no_phone_or_email(client):
    lead = _setup(phone="", email="")
    _owner_client(client)
    with patch("apps.payments.views.podium.send_message") as send:
        resp = client.post(reverse("order_send_pay_link", args=[lead.pk]))
    assert resp.status_code == 400
    assert "No phone or email" in resp.json()["error"]
    send.assert_not_called()


def test_requires_login(client):
    lead = _setup(phone="+15715551212")
    resp = client.post(reverse("order_send_pay_link", args=[lead.pk]))
    assert resp.status_code == 302


def test_requires_payments_access(client):
    lead = _setup(phone="+15715551212")
    client.force_login(UserFactory(role=User.Role.AGENT))
    resp = client.post(reverse("order_send_pay_link", args=[lead.pk]))
    assert resp.status_code in (302, 403)


def test_get_not_allowed(client):
    lead = _setup(phone="+15715551212")
    _owner_client(client)
    assert client.get(reverse("order_send_pay_link", args=[lead.pk])).status_code == 405
