"""`reconcile_open_charges` — catch payments the webhook never delivered (spec 2026-08-30 §10b).

After this spec `payment_intent.succeeded` is the only webhook success path, so a webhook that
never arrives means a customer paid and the order never booked. This hourly sweep makes
reconciliation independent of delivery.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger, tasks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry

pytestmark = pytest.mark.django_db

OLD = timedelta(minutes=30)  # older than RECONCILE_MIN_AGE


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _charge(plan, *, pi, kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"), age=OLD):
    c = plan.record_charge(kind=kind, amount=amount)
    c.stripe_payment_intent_id = pi
    c.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    Charge.objects.filter(pk=c.pk).update(updated_at=timezone.now() - age)
    return c


def _intent(pi, status="succeeded", amount=50000):
    return MagicMock(
        id=pi,
        status=status,
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def test_reconciles_a_succeeded_intent_and_books():
    plan = _plan()
    _charge(plan, pi="pi_ok")
    with (
        patch.object(
            tasks.services.stripe.PaymentIntent, "retrieve", return_value=_intent("pi_ok")
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        assert tasks.reconcile_open_charges() == 1
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED
    assert (
        JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED).count()
        == 1
    )


def test_leaves_a_young_charge_alone():
    plan = _plan()
    _charge(plan, pi="pi_young", age=timedelta(minutes=2))
    with patch.object(tasks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        assert tasks.reconcile_open_charges() == 0
    retrieve.assert_not_called()


@pytest.mark.parametrize(
    "status", ["requires_payment_method", "requires_confirmation", "processing"]
)
def test_leaves_an_still_open_intent_pending(status):
    """open_intent_for reuses exactly these — failing them would mint a duplicate Charge."""
    plan = _plan()
    charge = _charge(plan, pi="pi_open")
    with patch.object(
        tasks.services.stripe.PaymentIntent, "retrieve", return_value=_intent("pi_open", status)
    ):
        assert tasks.reconcile_open_charges() == 0
    charge.refresh_from_db()
    assert charge.status == Charge.Status.PENDING


def test_marks_a_canceled_intent_failed():
    plan = _plan()
    charge = _charge(plan, pi="pi_dead")
    with patch.object(
        tasks.services.stripe.PaymentIntent, "retrieve", return_value=_intent("pi_dead", "canceled")
    ):
        assert tasks.reconcile_open_charges() == 0
    charge.refresh_from_db()
    assert charge.status == Charge.Status.FAILED


def test_skips_a_charge_with_no_intent_id():
    plan = _plan()
    c = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    Charge.objects.filter(pk=c.pk).update(updated_at=timezone.now() - OLD)
    with patch.object(tasks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        assert tasks.reconcile_open_charges() == 0
    retrieve.assert_not_called()


def test_ignores_already_succeeded_charges():
    plan = _plan()
    c = _charge(plan, pi="pi_done")
    c.status = Charge.Status.SUCCEEDED
    c.save(update_fields=["status", "updated_at"])
    Charge.objects.filter(pk=c.pk).update(updated_at=timezone.now() - OLD)
    with patch.object(tasks.services.stripe.PaymentIntent, "retrieve") as retrieve:
        assert tasks.reconcile_open_charges() == 0
    retrieve.assert_not_called()


def test_is_idempotent_across_two_runs():
    plan = _plan()
    _charge(plan, pi="pi_ok")
    with (
        patch.object(
            tasks.services.stripe.PaymentIntent, "retrieve", return_value=_intent("pi_ok")
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        tasks.reconcile_open_charges()
        tasks.reconcile_open_charges()
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


def test_honours_the_batch_cap():
    plan = _plan(quote_total=Decimal("100000.00"))
    for i in range(tasks.RECONCILE_BATCH + 5):
        _charge(plan, pi=f"pi_{i}", kind=Charge.Kind.BALANCE, amount=Decimal("10.00"))
    seen = []

    def fake_retrieve(pi_id, **kwargs):
        seen.append(pi_id)
        return _intent(pi_id, "requires_payment_method")

    with patch.object(tasks.services.stripe.PaymentIntent, "retrieve", side_effect=fake_retrieve):
        tasks.reconcile_open_charges()
    assert len(seen) == tasks.RECONCILE_BATCH
