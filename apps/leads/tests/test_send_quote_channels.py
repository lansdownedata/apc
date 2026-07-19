"""send_quote delivers per selected channel; delivery stays best-effort."""

from unittest.mock import patch

import pytest

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def contact_with_both():
    from apps.contacts.factories import ContactFactory

    return ContactFactory(email="customer@example.com", phone="+13015550100")


@pytest.fixture
def new_lead(contact_with_both):
    lead = LeadFactory(status=Lead.Status.NEW, contact=contact_with_both)
    ReservationFactory(lead=lead, base_rate="500.00")
    return lead


def test_email_only_does_not_send_sms(new_lead):
    with (
        patch("apps.leads.services.send_html_email", return_value=True) as send_email,
        patch("apps.leads.services.podium.send_message") as send_sms,
    ):
        result = services.send_quote(new_lead, base_url="https://x.test", channels={"email"})
    assert result.ok
    assert send_email.call_count == 1
    assert send_sms.call_count == 0
    assert result.delivery["email"]["sent"] is True
    assert "sms" not in result.delivery


def test_sms_only_does_not_send_email(new_lead):
    with (
        patch("apps.leads.services.send_html_email") as send_email,
        patch("apps.leads.services.podium.send_message") as send_sms,
    ):
        result = services.send_quote(new_lead, base_url="https://x.test", channels={"sms"})
    assert send_email.call_count == 0
    assert send_sms.call_count == 1
    assert send_sms.call_args.kwargs["channel_type"] == "phone", "Podium calls SMS 'phone'"
    assert result.delivery["sms"]["sent"] is True
    assert "email" not in result.delivery


def test_both_is_the_default(new_lead):
    with (
        patch("apps.leads.services.send_html_email", return_value=True) as send_email,
        patch("apps.leads.services.podium.send_message") as send_sms,
    ):
        result = services.send_quote(new_lead, base_url="https://x.test")
    assert send_email.call_count == 1
    assert send_sms.call_count == 1
    assert set(result.delivery.keys()) == {"email", "sms"}


def test_a_delivery_failure_does_not_roll_back_the_transition(new_lead):
    with (
        patch("apps.leads.services.send_html_email", return_value=False),
        patch("apps.leads.services.podium.send_message", side_effect=Exception("podium down")),
    ):
        result = services.send_quote(new_lead, base_url="https://x.test")
    new_lead.refresh_from_db()
    assert result.ok, "the send is still a success — delivery is best-effort"
    assert new_lead.status == Lead.Status.QUOTED
    assert new_lead.quote_sent_at is not None
    assert new_lead.payment.quote_total > 0
    assert result.delivery["email"]["sent"] is False
    assert result.delivery["sms"]["sent"] is False
    assert result.delivery["sms"]["error"] == "podium down"


def test_email_channel_needs_an_email_address():
    from apps.contacts.factories import ContactFactory

    lead = LeadFactory(
        status=Lead.Status.NEW, contact=ContactFactory(email="", phone="+13015550100")
    )
    ReservationFactory(lead=lead, base_rate="500.00")
    result = services.send_quote(lead, base_url="https://x.test", channels={"email"})
    assert not result.ok
    assert "email" in result.error.lower()


def test_sms_channel_needs_a_phone_number():
    from apps.contacts.factories import ContactFactory

    lead = LeadFactory(
        status=Lead.Status.NEW, contact=ContactFactory(email="customer@example.com", phone="")
    )
    ReservationFactory(lead=lead, base_rate="500.00")
    result = services.send_quote(lead, base_url="https://x.test", channels={"sms"})
    assert not result.ok
    assert "phone" in result.error.lower()


def test_sms_still_works_without_an_email_address():
    from apps.contacts.factories import ContactFactory

    lead = LeadFactory(
        status=Lead.Status.NEW, contact=ContactFactory(email="", phone="+13015550100")
    )
    ReservationFactory(lead=lead, base_rate="500.00")
    with patch("apps.leads.services.podium.send_message"):
        result = services.send_quote(lead, base_url="https://x.test", channels={"sms"})
    assert result.ok


def test_email_still_works_without_a_phone_number():
    from apps.contacts.factories import ContactFactory

    lead = LeadFactory(
        status=Lead.Status.NEW, contact=ContactFactory(email="customer@example.com", phone="")
    )
    ReservationFactory(lead=lead, base_rate="500.00")
    with patch("apps.leads.services.send_html_email", return_value=True):
        result = services.send_quote(lead, base_url="https://x.test", channels={"email"})
    assert result.ok
