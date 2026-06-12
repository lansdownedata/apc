from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from apps.leads.models import Lead
from apps.payments import webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, PaymentPlan

pytestmark = pytest.mark.django_db


def _session_event(lead_id, pi="pi_1"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "metadata": {"lead_id": str(lead_id), "kind": "deposit"},
                "payment_intent": pi,
            }
        },
    }


def _saved_pm():
    return MagicMock(
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242"))
    )


def test_deposit_completed_saves_card_marks_paid_and_books():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.stripe.PaymentIntent, "retrieve", return_value=_saved_pm()):
        webhooks.process_stripe_event(_session_event(plan.lead_id))
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_brand == "visa"
    assert plan.card_last4 == "4242"
    assert plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    assert plan.charges.filter(kind=Charge.Kind.DEPOSIT, status=Charge.Status.SUCCEEDED).exists()


def test_balance_failed_marks_failed_and_alerts():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), balance_status=PaymentPlan.BalanceStatus.SCHEDULED
    )
    event = {
        "type": "payment_intent.payment_failed",
        "data": {
            "object": {
                "id": "pi_bal",
                "metadata": {"lead_id": str(plan.lead_id), "kind": "balance"},
                "last_payment_error": {"message": "Your card was declined."},
            }
        },
    }
    webhooks.process_stripe_event(event)
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.FAILED
    assert "declined" in plan.fail_reason.lower()
    plan.lead.refresh_from_db()
    assert plan.lead.has_alert is True


def test_view_returns_200_on_valid_signature(client):
    plan = PaymentPlanFactory()
    with (
        patch(
            "apps.payments.views.stripe.Webhook.construct_event",
            return_value=_session_event(plan.lead_id),
        ),
        patch.object(webhooks.stripe.PaymentIntent, "retrieve", return_value=_saved_pm()),
    ):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=abc",
        )
    assert resp.status_code == 200


def test_view_returns_400_on_bad_signature(client):
    err = stripe.error.SignatureVerificationError("bad sig", "sig")
    with patch("apps.payments.views.stripe.Webhook.construct_event", side_effect=err):
        resp = client.post(
            "/webhooks/stripe/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="x",
        )
    assert resp.status_code == 400
