from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.payments import ledger, services, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan

pytestmark = pytest.mark.django_db


def _session_event(lead_id):
    return {
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"lead_id": str(lead_id)}, "payment_intent": "pi_1"}},
    }


def _saved_pm():
    return MagicMock(
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242"))
    )


def test_deposit_webhook_posts_capture():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.stripe.PaymentIntent, "retrieve", return_value=_saved_pm()):
        webhooks.process_stripe_event(_session_event(plan.lead_id))
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")


def test_balance_charge_posts_capture():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"),
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
    )
    with patch.object(
        services.stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_bal")
    ):
        services.charge_balance(plan)
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")


def test_deposit_webhook_replay_is_idempotent():
    from apps.payments.models import JournalEntry

    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    with patch.object(webhooks.stripe.PaymentIntent, "retrieve", return_value=_saved_pm()):
        webhooks.process_stripe_event(_session_event(plan.lead_id))
        webhooks.process_stripe_event(_session_event(plan.lead_id))
    assert (
        JournalEntry.objects.filter(
            lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED
        ).count()
        == 1
    )
    bals = ledger.order_balances(plan.lead)
    assert bals["collected"] == Decimal("1335.00")
