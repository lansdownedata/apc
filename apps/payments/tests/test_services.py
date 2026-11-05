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
        services.stripe.PaymentIntent,
        "create",
        return_value=MagicMock(id="pi_1", status="succeeded"),
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


def test_charge_balance_when_the_webhook_lands_mid_call_records_one_payment(settings):
    """APC-126: Stripe can deliver `payment_intent.succeeded` before `PaymentIntent.create`
    has even returned to us. Whichever side gets there first finishes the one row; the other
    finds it done. One charge, one ledger entry, one balance-paid message."""
    from apps.messaging.factories import MessageRowFactory
    from apps.messaging.models import TouchPoint
    from apps.payments import webhooks
    from apps.payments.models import JournalEntry

    settings.TOUCHPOINTS_ENABLED = True
    plan = PaymentPlanFactory(
        quote_total=Decimal("2670.00"), stripe_customer_id="cus_1", stripe_payment_method_id="pm_1"
    )
    row = MessageRowFactory(trigger="balance_paid", recipients=["booking_contact"])

    def confirm_and_deliver_webhook(**kwargs):
        metadata = {k: str(v) for k, v in kwargs["metadata"].items()}
        intent = MagicMock(
            id="pi_race",
            status="succeeded",
            amount=133500,
            metadata=metadata,
            payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
        )
        event = {
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": "pi_race", "metadata": metadata}},
        }
        with patch.object(services.stripe.PaymentIntent, "retrieve", return_value=intent):
            webhooks.process_stripe_event(event)
        return intent

    with (
        patch.object(
            services.stripe.PaymentIntent, "create", side_effect=confirm_and_deliver_webhook
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        charge = services.charge_balance(plan)

    charge.refresh_from_db()
    assert charge.status == Charge.Status.SUCCEEDED
    assert charge.stripe_payment_intent_id == "pi_race"
    assert plan.charges.filter(kind=Charge.Kind.BALANCE).count() == 1
    assert (
        JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.BALANCE_CAPTURED).count()
        == 1
    )
    assert TouchPoint.objects.filter(message=row, lead=plan.lead).count() == 1
    plan.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID


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


def test_a_blank_secret_key_refuses_before_the_request_leaves(settings):
    """APC-121: stripe-python sends a request with `api_key=""` and only Stripe's 401 stops
    it. A blank key becomes None, which the library refuses locally."""
    settings.STRIPE_SECRET_KEY = ""
    client = services._stripe()

    with patch.object(stripe._http_client.HTTPClient, "request_with_retries") as sent:
        with pytest.raises(stripe.AuthenticationError, match="No API key provided"):
            client.Customer.create(name="x", idempotency_key="k")

    sent.assert_not_called()
