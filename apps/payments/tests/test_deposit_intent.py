"""The deposit is collected on our own page now — a PaymentIntent, not a Checkout Session.

Hosted Checkout (`create_deposit_checkout` + `quote_book`) is gone (spec 2026-08-30 §8).
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, PaymentPlan

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("2670.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _intents():
    return [MagicMock(id=f"pi_{n}", client_secret=f"pi_{n}_secret_x") for n in range(1, 4)]


def test_hosted_checkout_is_gone():
    assert not hasattr(services, "create_deposit_checkout")


def test_create_deposit_intent_returns_a_charge_and_secret():
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        charge, secret = services.create_deposit_intent(plan)
    assert charge.kind == Charge.Kind.DEPOSIT
    assert charge.amount == Decimal("1335.00")  # 50% of 2670
    assert secret == "pi_1_secret_x"


def test_create_deposit_intent_marks_the_deposit_requested():
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        services.create_deposit_intent(plan)
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.REQUESTED


def test_create_deposit_intent_saves_the_card_for_the_balance_cron():
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()) as create:
        services.create_deposit_intent(plan)
    kwargs = create.call_args.kwargs
    assert kwargs["amount"] == 133500
    assert kwargs["setup_future_usage"] == "off_session"
    assert kwargs["payment_method_types"] == ["card"]
    assert kwargs["metadata"]["kind"] == "deposit"
    assert kwargs["metadata"]["lead_id"] == str(plan.lead_id)


def test_create_deposit_intent_is_idempotent():
    """The public pay page calls this on every load — it must not pile up charges."""
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()) as create:
        first, first_secret = services.create_deposit_intent(plan)
        second, second_secret = services.create_deposit_intent(plan)
    assert second.pk == first.pk
    assert second_secret == first_secret
    assert plan.charges.count() == 1
    assert create.call_count == 1
