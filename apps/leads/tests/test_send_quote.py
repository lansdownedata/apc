from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.signing import BadSignature
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.integrations.podium import PodiumAPIError
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.models import PaymentPlan
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db

BASE_URL = "https://portal.example.com"


def test_deposit_token_round_trips():
    lead = LeadFactory()
    token = services.make_deposit_token(lead)
    assert services.read_deposit_token(token).pk == lead.pk


def test_deposit_token_rejects_tampering():
    lead = LeadFactory()
    token = services.make_deposit_token(lead)
    with pytest.raises(BadSignature):
        services.read_deposit_token(token + "x")


def _quotable_lead():
    """A NEW lead with one $185 reservation and a contact email."""
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email="rider@example.com"))
    TransferReservationFactory(lead=lead, rate=Decimal("185.00"))
    return lead


def test_send_quote_happy_path():
    lead = _quotable_lead()
    phone = lead.contact.phone
    with patch.object(services.podium, "send_message", return_value={}) as send:
        result = services.send_quote(lead, base_url=BASE_URL)

    assert result.ok and result.http_status == 200
    # Assert the link ROUND-TRIPS rather than byte-matching a freshly minted token:
    # signing.dumps embeds a second-resolution timestamp, so regenerating the token here
    # produces a different signature whenever this line and send_quote straddle a second
    # boundary. That made this test intermittently fail.
    assert result.link.startswith(f"{BASE_URL}/quote/")
    round_tripped = services.read_deposit_token(result.link.rsplit("/quote/", 1)[1].rstrip("/"))
    assert round_tripped.pk == lead.pk
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED
    plan = PaymentPlan.objects.get(lead=lead)
    assert plan.quote_total == Decimal("185.00")
    # Default channels = both: email goes through send_html_email (not Podium), SMS
    # through Podium with channel_type="phone" (Podium's own name for SMS).
    assert result.delivery == {
        "email": {"sent": True, "recipient": "rider@example.com", "error": None},
        "sms": {"sent": True, "recipient": phone, "error": None},
    }
    assert send.call_args.kwargs["channel_type"] == "phone"
    assert send.call_args.kwargs["identifier"] == phone


def test_send_quote_message_links_quote_page_not_stripe():
    lead = _quotable_lead()
    with patch.object(services.podium, "send_message", return_value={}) as send:
        services.send_quote(lead, base_url=BASE_URL)
    body = send.call_args.kwargs["body"]
    assert "/quote/" in body
    assert "checkout.stripe.com" not in body


def test_send_quote_stamps_sent_and_expiry_and_schedules_touchpoints():
    lead = _quotable_lead()
    with (
        patch.object(services.podium, "send_message", return_value={}),
        patch.object(services.touchpoints, "schedule_quote_sent") as scheduled,
    ):
        result = services.send_quote(lead, base_url=BASE_URL)
    assert result.ok
    lead.refresh_from_db()
    assert lead.quote_sent_at is not None
    assert lead.quote_expires_at is not None
    assert lead.quote_viewed_at is None
    scheduled.assert_called_once_with(lead)


def test_send_quote_blocks_when_no_reservations():
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email="a@b.com"))
    result = services.send_quote(lead, base_url=BASE_URL)
    assert not result.ok and result.http_status == 400
    lead.refresh_from_db()
    assert lead.status == Lead.Status.NEW


def test_send_quote_blocks_when_no_email():
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email=""))
    TransferReservationFactory(lead=lead, rate=Decimal("185.00"))
    result = services.send_quote(lead, base_url=BASE_URL)
    assert not result.ok and result.http_status == 400
    assert "email" in result.error.lower()


def test_send_quote_blocks_when_booked():
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(email="a@b.com"))
    TransferReservationFactory(lead=lead, rate=Decimal("185.00"))
    result = services.send_quote(lead, base_url=BASE_URL)
    assert not result.ok and result.http_status == 400


def test_send_quote_blocks_when_lost():
    lead = LeadFactory(status=Lead.Status.LOST, contact=ContactFactory(email="a@b.com"))
    TransferReservationFactory(lead=lead, rate=Decimal("185.00"))
    result = services.send_quote(lead, base_url=BASE_URL)
    assert not result.ok and result.http_status == 400
    lead.refresh_from_db()
    assert lead.status == Lead.Status.LOST


def test_read_deposit_token_missing_lead_raises():
    lead = LeadFactory()
    token = services.make_deposit_token(lead)
    lead.delete()
    with pytest.raises(Lead.DoesNotExist):
        services.read_deposit_token(token)


def test_send_quote_resend_keeps_quoted():
    lead = _quotable_lead()
    lead.status = Lead.Status.QUOTED
    lead.save(update_fields=["status"])
    with patch.object(services.podium, "send_message", return_value={}):
        result = services.send_quote(lead, base_url=BASE_URL)
    assert result.ok
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED


def test_send_quote_resend_clears_viewed_and_recomputes_expiry():
    lead = _quotable_lead()
    lead.status = Lead.Status.QUOTED
    lead.quote_viewed_at = timezone.now()
    lead.save(update_fields=["status", "quote_viewed_at"])
    with patch.object(services.podium, "send_message", return_value={}):
        result = services.send_quote(lead, base_url=BASE_URL)
    assert result.ok
    lead.refresh_from_db()
    assert lead.quote_viewed_at is None
    assert lead.quote_expires_at is not None


def test_send_quote_degrades_when_podium_fails():
    lead = _quotable_lead()
    with patch.object(
        services.podium,
        "send_message",
        side_effect=PodiumAPIError("403 missing write_messages"),
    ):
        result = services.send_quote(lead, base_url=BASE_URL)
    # quote still went through; only delivery failed
    assert result.ok and result.http_status == 200
    assert result.delivery["sms"]["sent"] is False
    assert "403" in result.delivery["sms"]["error"]
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED


# ---------------------------------------------------------------------------
# compute_quote_expiry
# ---------------------------------------------------------------------------


@override_settings(QUOTE_EXPIRY_DAYS_BEFORE_PICKUP=14)
def test_compute_quote_expiry_pickup_far_out_returns_cutoff():
    lead = _quotable_lead()
    pickup_date = (timezone.now() + timedelta(days=30)).date()
    TransferReservationFactory(lead=lead, pickup_date=pickup_date, pickup_time=time(9, 0))
    expiry = services.compute_quote_expiry(lead)
    expected = timezone.make_aware(datetime.combine(pickup_date, time(9, 0))) - timedelta(days=14)
    assert expiry == expected


@override_settings(QUOTE_EXPIRY_DAYS_BEFORE_PICKUP=14)
def test_compute_quote_expiry_late_pickup_returns_pickup_itself():
    lead = _quotable_lead()
    pickup_date = (timezone.now() + timedelta(days=5)).date()
    TransferReservationFactory(lead=lead, pickup_date=pickup_date, pickup_time=time(9, 0))
    expiry = services.compute_quote_expiry(lead)
    expected = timezone.make_aware(datetime.combine(pickup_date, time(9, 0)))
    assert expiry == expected


@override_settings(QUOTE_EXPIRY_DAYS_BEFORE_PICKUP=14)
def test_compute_quote_expiry_no_pickup_date_returns_now_plus_days():
    lead = _quotable_lead()
    before = timezone.now()
    expiry = services.compute_quote_expiry(lead)
    after = timezone.now()
    assert before + timedelta(days=14) <= expiry <= after + timedelta(days=14)


# ---------------------------------------------------------------------------
# View tests (Task 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_send_quote_view_requires_login(client):
    lead = _quotable_lead()
    resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_send_quote_view_happy_path(client, agent):
    lead = _quotable_lead()
    client.force_login(agent)
    with patch.object(services.podium, "send_message", return_value={}):
        resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"]
    # Round-trip rather than byte-match: signing.dumps embeds a second-resolution
    # timestamp, so regenerating the token here races across second boundaries.
    # (Same defect as 98e9e5f, which fixed only the service-level twin of this test.)
    assert "/quote/" in data["link"]
    round_tripped = services.read_deposit_token(data["link"].rsplit("/quote/", 1)[1].rstrip("/"))
    assert round_tripped.pk == lead.pk
    assert data["delivery"]["email"]["sent"] is True
    assert data["delivery"]["sms"]["sent"] is True


def test_send_quote_view_precondition_returns_400(client, agent):
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email=""))
    TransferReservationFactory(lead=lead, rate=Decimal("185.00"))
    client.force_login(agent)
    resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Task 4: Public deposit success/cancel pages
# ---------------------------------------------------------------------------


def test_deposit_success_page_public_no_pii(client):
    lead = _quotable_lead()
    lead.contact.name = "Jane Privatename"
    lead.contact.save()
    PaymentPlan.objects.create(lead=lead, quote_total=Decimal("185.00"))
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_deposit_success", args=[token]))  # no login
    assert resp.status_code == 200
    body = resp.content.decode()
    assert lead.quote_no in body
    assert "Jane Privatename" not in body
    assert (lead.contact.email or "x@x") not in body


def test_deposit_cancel_page_public(client):
    lead = _quotable_lead()
    token = services.make_deposit_token(lead)
    resp = client.get(reverse("quote_deposit_cancel", args=[token]))
    assert resp.status_code == 200
    assert lead.quote_no in resp.content.decode()


def test_deposit_page_rejects_bad_token(client):
    resp = client.get(reverse("quote_deposit_success", args=["not-a-real-token"]))
    assert resp.status_code == 404


def test_deposit_cancel_page_rejects_bad_token(client):
    resp = client.get(reverse("quote_deposit_cancel", args=["not-a-real-token"]))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Task 5: Render tests — status-aware Send quote button
# ---------------------------------------------------------------------------


def test_detail_shows_send_quote_for_new(client, agent):
    lead = _quotable_lead()
    client.force_login(agent)
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Send quote" in body
    assert "not wired up yet" not in body  # old preview copy is gone


def test_detail_shows_resend_for_quoted(client, agent):
    lead = _quotable_lead()
    lead.status = Lead.Status.QUOTED
    lead.save(update_fields=["status"])
    client.force_login(agent)
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Resend deposit request" in body
