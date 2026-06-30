from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.signing import BadSignature
from django.urls import reverse

from apps.contacts.factories import ContactFactory
from apps.integrations.podium import PodiumAPIError
from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services as payment_services
from apps.payments.models import PaymentPlan
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


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
    TransferReservationFactory(lead=lead, base_rate=Decimal("185.00"))
    return lead


def _stripe_ok():
    """Patch the Stripe SDK calls create_deposit_checkout makes (customer + session)."""
    return [
        patch.object(
            payment_services.stripe.Customer, "create", return_value=MagicMock(id="cus_1")
        ),
        patch.object(
            payment_services.stripe.checkout.Session,
            "create",
            return_value=MagicMock(url="https://checkout.stripe/abc"),
        ),
    ]


def test_send_quote_happy_path():
    lead = _quotable_lead()
    cust, sess = _stripe_ok()
    with cust, sess, patch.object(services.podium, "send_message", return_value={}) as send:
        result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")

    assert result.ok and result.http_status == 200
    assert result.link == "https://checkout.stripe/abc"
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED
    plan = PaymentPlan.objects.get(lead=lead)
    assert plan.quote_total == Decimal("185.00")
    assert plan.deposit_status == PaymentPlan.DepositStatus.REQUESTED
    assert result.delivery == {"sent": True, "recipient": "rider@example.com", "error": None}
    assert send.call_args.kwargs["channel_type"] == "email"
    assert send.call_args.kwargs["identifier"] == "rider@example.com"


def test_send_quote_blocks_when_no_reservations():
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email="a@b.com"))
    result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    assert not result.ok and result.http_status == 400
    lead.refresh_from_db()
    assert lead.status == Lead.Status.NEW


def test_send_quote_blocks_when_no_email():
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email=""))
    TransferReservationFactory(lead=lead, base_rate=Decimal("185.00"))
    result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    assert not result.ok and result.http_status == 400
    assert "email" in result.error.lower()


def test_send_quote_blocks_when_booked():
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(email="a@b.com"))
    TransferReservationFactory(lead=lead, base_rate=Decimal("185.00"))
    result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    assert not result.ok and result.http_status == 400


def test_send_quote_resend_keeps_quoted():
    lead = _quotable_lead()
    lead.status = Lead.Status.QUOTED
    lead.save(update_fields=["status"])
    cust, sess = _stripe_ok()
    with cust, sess, patch.object(services.podium, "send_message", return_value={}):
        result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    assert result.ok
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED


def test_send_quote_surfaces_stripe_message_and_stays_new():
    lead = _quotable_lead()
    cust = patch.object(
        payment_services.stripe.Customer, "create", return_value=MagicMock(id="cus_1")
    )
    sess = patch.object(
        payment_services.stripe.checkout.Session,
        "create",
        side_effect=stripe.error.StripeError("Your card was declined."),
    )
    with cust, sess:
        result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    assert not result.ok and result.http_status == 502
    assert "Your card was declined." in result.error
    lead.refresh_from_db()
    assert lead.status == Lead.Status.NEW


def test_send_quote_degrades_when_podium_fails():
    lead = _quotable_lead()
    cust, sess = _stripe_ok()
    with (
        cust,
        sess,
        patch.object(
            services.podium,
            "send_message",
            side_effect=PodiumAPIError("403 missing write_messages"),
        ),
    ):
        result = services.send_quote(lead, success_url="https://ok", cancel_url="https://no")
    # quote still went through; only delivery failed
    assert result.ok and result.http_status == 200
    assert result.link == "https://checkout.stripe/abc"
    assert result.delivery["sent"] is False
    assert "403" in result.delivery["error"]
    lead.refresh_from_db()
    assert lead.status == Lead.Status.QUOTED


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
    cust, sess = _stripe_ok()
    with cust, sess, patch.object(services.podium, "send_message", return_value={}):
        resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] and data["link"] == "https://checkout.stripe/abc"
    assert data["delivery"]["sent"] is True


def test_send_quote_view_precondition_returns_400(client, agent):
    lead = LeadFactory(status=Lead.Status.NEW, contact=ContactFactory(email=""))
    TransferReservationFactory(lead=lead, base_rate=Decimal("185.00"))
    client.force_login(agent)
    resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()


def test_send_quote_view_stripe_failure_returns_502(client, agent):
    lead = _quotable_lead()
    client.force_login(agent)
    cust = patch.object(payment_services.stripe.Customer, "create", return_value=MagicMock(id="c"))
    sess = patch.object(
        payment_services.stripe.checkout.Session,
        "create",
        side_effect=stripe.error.StripeError("No such price"),
    )
    with cust, sess:
        resp = client.post(reverse("lead_send_quote", args=[lead.pk]))
    assert resp.status_code == 502
    assert "No such price" in resp.json()["error"]


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


def test_deposit_page_rejects_bad_token(client):
    resp = client.get(reverse("quote_deposit_success", args=["not-a-real-token"]))
    assert resp.status_code == 404
