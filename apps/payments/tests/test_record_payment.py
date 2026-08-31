"""`record_payment` — one reconcile for all three surfaces (spec 2026-08-30 §6).

Kind-aware so the deposit can use it too: the customer pay page, the staff Payment Element and
the webhook all land here. `record_admin_payment` is gone, not aliased.

The three plan-flag edges below are what let §7 drop the manual `deposit_status=PAID` /
`balance_status=SCHEDULED` step the old `_deposit_completed` did by hand — `record_payment`
already reaches them through `sync_plan_from_collected`.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger, services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    status = kwargs.pop("lead_status", Lead.Status.QUOTED)
    lead = kwargs.pop("lead", None) or LeadFactory(status=status)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _intent(pi_id="pi_1", amount=50000, status="succeeded"):
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _reconcile(plan, pi_id="pi_1", *, kind=Charge.Kind.BALANCE, intent=None):
    with (
        patch.object(
            services.stripe.PaymentIntent, "retrieve", return_value=intent or _intent(pi_id)
        ),
        patch("apps.integrations.la_sync.push_lead_bookings"),
    ):
        return services.record_payment(plan, pi_id, kind=kind)


def test_record_admin_payment_is_gone():
    assert not hasattr(services, "record_admin_payment")


# --- ledger kind ------------------------------------------------------------
def test_deposit_posts_a_deposit_capture():
    plan = _plan()
    _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    entries = JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED)
    assert entries.count() == 1


def test_balance_posts_a_balance_capture():
    plan = _plan()
    _reconcile(plan, kind=Charge.Kind.BALANCE)
    entries = JournalEntry.objects.filter(lead=plan.lead, kind=JournalEntry.Kind.BALANCE_CAPTURED)
    assert entries.count() == 1


# --- charge resolution ------------------------------------------------------
def test_reuses_the_existing_charge_for_that_intent():
    plan = _plan()
    charge = plan.record_charge(kind=Charge.Kind.DEPOSIT, amount=Decimal("500.00"))
    charge.stripe_payment_intent_id = "pi_1"
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    result = _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    assert result.pk == charge.pk
    assert plan.charges.count() == 1


def test_creates_a_charge_of_the_right_kind_when_absent():
    plan = _plan()
    charge = _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    assert charge.kind == Charge.Kind.DEPOSIT
    assert charge.amount == Decimal("500.00")  # from the intent's 50000 cents


# --- idempotency ------------------------------------------------------------
def test_is_idempotent_on_a_second_call():
    """The webhook and the customer's own complete POST can both fire."""
    plan = _plan()
    _reconcile(plan)
    _reconcile(plan)
    assert plan.charges.count() == 1
    assert ledger.order_balances(plan.lead)["collected"] == Decimal("500.00")


# --- side effects -----------------------------------------------------------
def test_saves_the_card_and_books_a_quoted_lead():
    plan = _plan()
    _reconcile(plan)
    plan.refresh_from_db()
    plan.lead.refresh_from_db()
    assert plan.stripe_payment_method_id == "pm_1"
    assert plan.card_brand == "visa"
    assert plan.card_last4 == "4242"
    assert plan.lead.status == Lead.Status.BOOKED


def test_books_a_new_lead_too():
    plan = _plan(lead_status=Lead.Status.NEW)
    _reconcile(plan)
    plan.lead.refresh_from_db()
    assert plan.lead.status == Lead.Status.BOOKED


def test_refuses_an_unfinished_intent():
    plan = _plan()
    with pytest.raises(services.PaymentError):
        _reconcile(plan, intent=_intent(status="requires_payment_method"))


# --- plan flag edges (these are what let §7 drop its manual flag-setting) ----
def test_deposit_under_total_schedules_the_balance():
    plan = _plan(quote_total=Decimal("1000.00"), deposit_pct=50)
    _reconcile(plan, kind=Charge.Kind.DEPOSIT)  # 50000 cents = the 500.00 deposit
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.balance_status == PaymentPlan.BalanceStatus.SCHEDULED


def test_a_hundred_percent_deposit_marks_both_paid():
    plan = _plan(quote_total=Decimal("500.00"), deposit_pct=100)
    _reconcile(plan, kind=Charge.Kind.DEPOSIT)
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.PAID
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID


def test_a_zero_total_flips_neither_flag():
    plan = _plan(quote_total=Decimal("0.00"), deposit_pct=50)
    _reconcile(plan)
    plan.refresh_from_db()
    assert plan.deposit_status == PaymentPlan.DepositStatus.UNSENT
    assert plan.balance_status == PaymentPlan.BalanceStatus.NA
