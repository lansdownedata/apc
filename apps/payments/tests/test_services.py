from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe

from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, PaymentPlan

pytestmark = pytest.mark.django_db


# --- customer --------------------------------------------------------------
def test_get_or_create_customer_creates_and_stores():
    plan = PaymentPlanFactory()
    with patch.object(
        services.stripe.Customer, "create", return_value=MagicMock(id="cus_1")
    ) as create:
        cid = services.get_or_create_customer(plan)
    assert cid == "cus_1"
    plan.refresh_from_db()
    assert plan.stripe_customer_id == "cus_1"
    create.assert_called_once()


def test_get_or_create_customer_reuses_existing():
    plan = PaymentPlanFactory(stripe_customer_id="cus_existing")
    with patch.object(services.stripe.Customer, "create") as create:
        assert services.get_or_create_customer(plan) == "cus_existing"
    create.assert_not_called()


# --- deposit intent (hosted Checkout removed 2026-08-30 — see test_deposit_intent.py) -----


# --- balance charge --------------------------------------------------------
def test_charge_balance_success():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), stripe_customer_id="cus_1", stripe_payment_method_id="pm_1"
    )
    with patch.object(
        services.stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_1")
    ) as create:
        charge = services.charge_balance(plan)
    assert charge.status == Charge.Status.SUCCEEDED
    assert charge.stripe_payment_intent_id == "pi_1"
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID
    kwargs = create.call_args.kwargs
    assert kwargs["amount"] == 133500
    assert kwargs["off_session"] is True
    assert kwargs["idempotency_key"] == charge.idempotency_key


def test_charge_balance_card_error_fails_and_alerts():
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), stripe_customer_id="cus_1", stripe_payment_method_id="pm_1"
    )
    err = stripe.error.CardError("Your card was declined.", None, "card_declined")
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=err):
        charge = services.charge_balance(plan)
    assert charge.status == Charge.Status.FAILED
    assert "declined" in charge.failure_reason.lower()
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.FAILED
    assert plan.fail_reason
    assert plan.lead.has_alert is True
    from apps.notifications.models import Notification

    assert Notification.objects.filter(
        lead=plan.lead, kind=Notification.Kind.BALANCE_FAILED
    ).exists()
