"""`open_intent_for` — one open PaymentIntent per (plan, kind, amount).

`PaymentPlan.record_charge()` mints a new row on every call, so a public endpoint calling it
per page load would let anyone holding a valid quote token pile up unbounded PENDING charges.
Reuse is what makes the customer pay page safe to hit repeatedly (spec 2026-08-30 §6).
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _intents():
    """A fresh MagicMock per PaymentIntent.create call, with distinct ids."""
    return [MagicMock(id=f"pi_{n}", client_secret=f"pi_{n}_secret_x") for n in range(1, 6)]


def test_reuses_a_pending_charge_at_the_same_amount():
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()) as create:
        first, first_secret = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
        second, second_secret = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
    assert second.pk == first.pk
    assert second_secret == first_secret
    assert plan.charges.count() == 1
    assert create.call_count == 1


def test_does_not_retrieve_the_intent_from_stripe():
    """The reuse path is local-only — no Stripe round-trip on a public endpoint."""
    plan = _plan()
    with (
        patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()),
        patch.object(services.stripe.PaymentIntent, "retrieve") as retrieve,
    ):
        services.open_intent_for(plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
        services.open_intent_for(plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    retrieve.assert_not_called()


def test_a_changed_amount_creates_a_new_charge_and_intent():
    """A partial staff charge landed between page loads — the old intent is abandoned."""
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()) as create:
        first, _ = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
        second, _ = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("300.00")
        )
    assert second.pk != first.pk
    assert plan.charges.count() == 2
    assert create.call_count == 2
    first.refresh_from_db()
    assert first.status == Charge.Status.PENDING  # abandoned, not cancelled


def test_a_succeeded_charge_is_never_reused():
    plan = _plan()
    spent = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    spent.stripe_payment_intent_id = "pi_spent"
    spent.status = Charge.Status.SUCCEEDED
    spent.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        charge, _ = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
    assert charge.pk != spent.pk


def test_a_failed_charge_is_never_reused():
    plan = _plan()
    dead = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    dead.stripe_payment_intent_id = "pi_dead"
    dead.status = Charge.Status.FAILED
    dead.save(update_fields=["stripe_payment_intent_id", "status", "updated_at"])
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        charge, _ = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
    assert charge.pk != dead.pk


def test_a_deposit_charge_is_not_handed_back_for_a_balance_request():
    plan = _plan()
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        deposit, _ = services.open_intent_for(
            plan, kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00")
        )
        balance, _ = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
    assert balance.pk != deposit.pk
    assert balance.kind == Charge.Kind.BALANCE


def test_a_charge_without_an_intent_id_is_not_reused():
    """record_charge() writes the row before Stripe is called; a crash in between must not
    leave a charge that looks reusable but has no intent behind it."""
    plan = _plan()
    orphan = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("500.00"))
    with patch.object(services.stripe.PaymentIntent, "create", side_effect=_intents()):
        charge, secret = services.open_intent_for(
            plan, kind=Charge.Kind.BALANCE, amount=Decimal("500.00")
        )
    assert charge.pk != orphan.pk
    assert secret
