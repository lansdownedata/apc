"""APC-93 — "Send terms": the office texts or emails a staff/phone booking the link to
accept the T&Cs, which closes the contract_signed task when they do."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.integrations.podium import PodiumAPIError
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import Conversation, Message
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory

pytestmark = pytest.mark.django_db
SEND = "apps.integrations.podium.send_message"


def _setup(status=Lead.Status.BOOKED, **contact_kwargs):
    contact = ContactFactory(**contact_kwargs)
    lead = LeadFactory(status=status, contact=contact)
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    return lead


def _send(lead):
    return services.send_terms_link(lead, base_url="https://apc.example.com", sender_name="Sam")


def test_send_terms_link_sends_by_sms_and_records_it():
    lead = _setup(phone="+15715551212", email="rider@example.com")

    with patch(SEND, return_value={"uid": "t1"}) as send:
        assert _send(lead) == "sms"

    body = send.call_args.kwargs["body"]
    assert lead.quote_no in body and "/terms/" in body
    msg = Conversation.objects.get(contact=lead.contact).messages.get()
    assert (msg.direction, msg.channel) == (Message.Direction.OUT, Message.Channel.SMS)


def test_send_terms_link_falls_back_to_email():
    lead = _setup(phone="", email="rider@example.com")

    with patch(SEND, return_value={"uid": "t2"}) as send:
        assert _send(lead) == "email"

    assert send.call_args.kwargs["channel_type"] == "email"


@override_settings(OUTBOUND_ENABLED=False)
def test_send_terms_link_is_refused_while_outbound_is_off():
    lead = _setup(phone="+15715551212")

    with patch(SEND) as send, pytest.raises(services.PaymentError):
        _send(lead)

    send.assert_not_called()


def test_send_terms_link_is_refused_once_terms_are_accepted():
    lead = _setup(phone="+15715551212")
    Lead.objects.filter(pk=lead.pk).update(accepted_terms_at=timezone.now())
    lead.refresh_from_db()

    with patch(SEND) as send, pytest.raises(services.PaymentError, match="already accepted"):
        _send(lead)

    send.assert_not_called()


def test_send_terms_link_is_refused_for_an_order_that_is_not_booked():
    lead = _setup(status=Lead.Status.QUOTED, phone="+15715551212")

    with patch(SEND) as send, pytest.raises(services.PaymentError, match="booked"):
        _send(lead)

    send.assert_not_called()


def test_the_endpoint_returns_the_channel_for_any_staff_member(client):
    """Customer Service owns contract_signed; sending terms moves no money, so it is not
    gated on payments access."""
    lead = _setup(phone="+15715551212")
    client.force_login(UserFactory(role=User.Role.AGENT))

    with patch(SEND, return_value={"uid": "t3"}):
        resp = client.post(reverse("order_send_terms_link", args=[lead.pk]))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "channel": "sms"}


def test_podium_down_is_a_502(client):
    lead = _setup(phone="+15715551212")
    client.force_login(UserFactory())

    with patch(SEND, side_effect=PodiumAPIError(500, "boom")):
        resp = client.post(reverse("order_send_terms_link", args=[lead.pk]))

    assert resp.status_code == 502
    assert resp.json()["ok"] is False


def test_the_endpoint_requires_login_and_post(client):
    lead = _setup(phone="+15715551212")
    url = reverse("order_send_terms_link", args=[lead.pk])

    assert client.post(url).status_code == 302
    client.force_login(UserFactory())
    assert client.get(url).status_code == 405


def test_the_button_shows_only_for_a_booked_order_without_acceptance(client):
    client.force_login(UserFactory())
    pending = _setup(phone="+15715551212")
    accepted = _setup(phone="+15715551212")
    Lead.objects.filter(pk=accepted.pk).update(accepted_terms_at=timezone.now())
    quoted = _setup(status=Lead.Status.QUOTED, phone="+15715551212")

    def page(lead):
        return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert reverse("order_send_terms_link", args=[pending.pk]) in page(pending)
    assert reverse("order_send_terms_link", args=[accepted.pk]) not in page(accepted)
    assert reverse("order_send_terms_link", args=[quoted.pk]) not in page(quoted)


@pytest.mark.parametrize("name", ["order_send_terms_link", "order_send_pay_link"])
def test_a_podium_network_failure_is_a_502_not_a_500(client, name):
    import requests

    lead = _setup(phone="+15715551212")
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))

    with patch(SEND, side_effect=requests.ConnectionError("unreachable")):
        resp = client.post(reverse(name, args=[lead.pk]))

    assert resp.status_code == 502
    assert resp.json()["ok"] is False
