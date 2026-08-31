"""Every intent we create is locked to `card` server-side (spec 2026-08-30 §4).

Belt-and-braces with the client-side `paymentMethodTypes: ['card']`: this half is the only
one a webhook-driven or off-session charge honours, and it makes an accidental change to the
Dashboard's enabled methods unable to surface a new method on any surface.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def test_admin_payment_intent_is_card_only():
    plan = _plan()
    with patch.object(
        services.stripe.PaymentIntent,
        "create",
        return_value=MagicMock(id="pi_1", client_secret="pi_1_secret_x"),
    ) as create:
        services.create_admin_payment_intent(plan, Decimal("400.00"))
    assert create.call_args.kwargs["payment_method_types"] == ["card"]


def test_balance_charge_is_card_only():
    plan = _plan(stripe_payment_method_id="pm_1")
    with patch.object(
        services.stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_1")
    ) as create:
        services.charge_balance(plan)
    assert create.call_args.kwargs["payment_method_types"] == ["card"]


def test_charge_saved_card_is_card_only():
    plan = _plan(stripe_payment_method_id="pm_1")
    intent = MagicMock(
        id="pi_1",
        status="succeeded",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )
    with (
        patch.object(services.stripe.PaymentIntent, "create", return_value=intent) as create,
        patch.object(services.stripe.PaymentIntent, "retrieve", return_value=intent),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        services.charge_saved_card(plan, Decimal("400.00"))
    assert create.call_args.kwargs["payment_method_types"] == ["card"]


def test_setup_intent_is_card_only():
    plan = _plan()
    with patch.object(
        services.stripe.SetupIntent, "create", return_value=MagicMock(client_secret="seti_1_secret")
    ) as create:
        services.create_setup_intent(plan)
    assert create.call_args.kwargs["payment_method_types"] == ["card"]
