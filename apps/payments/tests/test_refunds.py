from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.models import Lead
from apps.payments import ledger, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def _paid_plan():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    plan.charges.create(
        kind=Charge.Kind.BALANCE, amount=Decimal("1335.00"),
        status=Charge.Status.SUCCEEDED, stripe_payment_intent_id="pi_bal",
        idempotency_key="seed-bal",
    )
    ledger.post_capture(
        lead=plan.lead, amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED, idempotency_key="cap-bal",
    )
    return plan


def test_refund_payment_posts_stripe_and_ledger():
    plan = _paid_plan()
    with patch.object(services.stripe.Refund, "create", return_value=MagicMock(id="re_1")):
        refunded = services.refund_payment(plan, Decimal("400.00"))
    assert refunded == Decimal("400.00")
    assert plan.charges.filter(kind=Charge.Kind.REFUND, stripe_refund_id="re_1").exists()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("935.00")


def test_mark_paid_offline_posts_capture():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"))
    services.mark_paid_offline(plan, Decimal("500.00"))
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


def test_refund_endpoint_requires_payment_access(client):
    plan = _paid_plan()
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    resp = client.post(reverse("order_refund", args=[plan.lead_id]), {"amount": "100.00"})
    assert resp.status_code == 403


def test_cancel_and_refund_cancels_and_reverses(client):
    plan = _paid_plan()
    res = TransferReservationFactory(lead=plan.lead, base_rate=Decimal("1335.00"))
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    with patch.object(services.stripe.Refund, "create", return_value=MagicMock(id="re_2")):
        resp = client.post(reverse("order_cancel_refund", args=[plan.lead_id]))
    assert resp.status_code in (200, 302)
    plan.lead.refresh_from_db()
    res.refresh_from_db()
    assert plan.lead.status == Lead.Status.LOST
    assert res.revenue_status == res.RevenueStatus.REVERSED


def test_refund_caps_at_captured_no_double_refund():
    plan = _paid_plan()  # one succeeded balance charge of 1335 on pi_bal
    with patch.object(
        services.stripe.Refund, "create",
        side_effect=[MagicMock(id="re_a"), MagicMock(id="re_b")],
    ):
        first = services.refund_payment(plan, Decimal("1335.00"))
        second = services.refund_payment(plan, Decimal("1335.00"))
    assert first == Decimal("1335.00")
    assert second == Decimal("0.00")  # already fully refunded — no second Stripe refund
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("0.00")  # never negative


def test_refund_endpoint_rejects_bad_amount(client):
    plan = _paid_plan()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    resp = client.post(reverse("order_refund", args=[plan.lead_id]), {"amount": "abc"})
    assert resp.status_code in (200, 302)  # graceful, not a 500
