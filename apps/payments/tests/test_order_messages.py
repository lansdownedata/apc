"""APC-26 step 4 — what the customer is told, and when.

The flow's whole premise is that paying no longer means booked, so every customer-facing
moment has to stop saying it does: the checkout page, and the two messages that finally
resolve the wait.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import Lead
from apps.leads.services import make_deposit_token
from apps.messaging.models import NotificationConfig, TouchPoint
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, PaymentPlan

pytestmark = pytest.mark.django_db


def _authorized_intent(pi="pi_1", amount=133500):
    return MagicMock(
        id=pi,
        status="requires_capture",
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _captured_intent(pi="pi_1", amount=133500):
    return MagicMock(
        id=pi,
        status="succeeded",
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _engaged():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.status = Lead.Status.ENGAGED
    plan.lead.quote_expires_at = timezone.now() + timedelta(days=3)
    plan.lead.save(update_fields=["status", "quote_expires_at"])
    plan.deposit_status = PaymentPlan.DepositStatus.AUTHORIZED
    plan.save(update_fields=["deposit_status"])
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = "pi_1"
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = timezone.now()
    charge.capture_expires_at = timezone.now() + timedelta(days=7)
    charge.save()
    return plan


# --- the checkout page can no longer say "booked" -------------------------------------


def test_success_page_says_held_not_received(client):
    plan = _engaged()
    resp = client.get(reverse("quote_deposit_success", args=[make_deposit_token(plan.lead)]))

    body = resp.content.decode().lower()
    assert resp.status_code == 200
    # the customer must not leave believing the trip is booked
    assert "deposit received" not in body
    assert "processing your booking" not in body
    assert "authorized" in body or "hold" in body
    assert "confirm" in body


def test_a_paid_in_full_order_still_reads_as_paid(client):
    """A captured balance is money actually taken — that copy stays true."""
    plan = PaymentPlanFactory(quote_total=Decimal("100.00"))
    plan.lead.status = Lead.Status.BOOKED
    plan.lead.save(update_fields=["status"])
    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal("100.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="seed-full",
    )

    resp = client.get(reverse("quote_deposit_success", args=[make_deposit_token(plan.lead)]))

    assert "paid in full" in resp.content.decode().lower()


def test_the_3ds_return_path_records_an_authorization_not_a_payment(client):
    """A 3-D Secure customer never runs the inline complete POST — this redirect is their
    only inline path, and a manual-capture deposit is `requires_capture` here."""
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.lead.status = Lead.Status.QUOTED
    plan.lead.save(update_fields=["status"])
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=plan.deposit_amount)
    charge.stripe_payment_intent_id = "pi_1"
    charge.save(update_fields=["stripe_payment_intent_id"])

    with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_authorized_intent()):
        client.get(
            reverse("quote_deposit_success", args=[make_deposit_token(plan.lead)]),
            {"payment_intent": "pi_1", "redirect_status": "succeeded"},
        )

    plan.lead.refresh_from_db()
    charge.refresh_from_db()
    assert plan.lead.status == Lead.Status.ENGAGED
    assert charge.status == Charge.Status.AUTHORIZED


# --- the two messages that end the wait ----------------------------------------------


def test_confirming_queues_the_order_confirmation():
    plan = _engaged()
    with (
        patch.object(services.stripe.PaymentIntent, "capture"),
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=_captured_intent()),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.confirm_order(plan.lead, user=None)

    assert TouchPoint.objects.filter(lead=plan.lead, kind=TouchPoint.Kind.ORDER_CONFIRMED).exists()


def test_cancelling_queues_the_cancellation_notice():
    plan = _engaged()
    with patch.object(services.stripe.PaymentIntent, "cancel"):
        services.cancel_order(plan.lead, user=None, reason="No coach available")

    assert TouchPoint.objects.filter(lead=plan.lead, kind=TouchPoint.Kind.ORDER_CANCELLED).exists()


def test_the_cancellation_survives_the_lost_cleanup():
    """Marking the lead LOST cancels its pending touch-points — the message explaining why
    must not be swept away with them."""
    plan = _engaged()
    with patch.object(services.stripe.PaymentIntent, "cancel"):
        services.cancel_order(plan.lead, user=None, reason="x")

    tp = TouchPoint.objects.get(lead=plan.lead, kind=TouchPoint.Kind.ORDER_CANCELLED)
    assert tp.status == TouchPoint.Status.SCHEDULED


def test_a_lost_lead_still_sends_its_cancellation(settings):
    """`_process` skips a LOST lead outright — the one message that exists *because* it is
    lost has to be the exception."""
    settings.PUBLIC_BASE_URL = "https://apc.example.com"
    plan = _engaged()
    plan.lead.contact.email = "rider@example.com"
    plan.lead.contact.save()
    with patch.object(services.stripe.PaymentIntent, "cancel"):
        services.cancel_order(plan.lead, user=None, reason="x")

    tp = TouchPoint.objects.get(lead=plan.lead, kind=TouchPoint.Kind.ORDER_CANCELLED)
    from apps.messaging import touchpoints

    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}) as send:
        assert touchpoints._process(tp) is True
    send.assert_called()


def test_both_messages_can_be_switched_off():
    cfg = NotificationConfig.load()
    cfg.order_confirmed_enabled = False
    cfg.order_cancelled_enabled = False
    cfg.save()
    plan = _engaged()

    with patch.object(services.stripe.PaymentIntent, "cancel"):
        services.cancel_order(plan.lead, user=None, reason="x")

    assert not TouchPoint.objects.filter(
        lead=plan.lead, kind=TouchPoint.Kind.ORDER_CANCELLED
    ).exists()
