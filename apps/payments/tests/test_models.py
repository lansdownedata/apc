from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError

from apps.leads.factories import LeadFactory
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge, PaymentPlan
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


# --- amounts ---------------------------------------------------------------
def test_deposit_and_balance_split_50_50():
    plan = PaymentPlanFactory(quote_total=Decimal("2670.00"), deposit_pct=50)
    assert plan.deposit_amount == Decimal("1335.00")
    assert plan.balance_amount == Decimal("1335.00")


def test_amounts_round_to_cents():
    plan = PaymentPlanFactory(quote_total=Decimal("1001.00"), deposit_pct=50)
    assert plan.deposit_amount == Decimal("500.50")
    assert plan.balance_amount == Decimal("500.50")


def test_snapshot_total_pulls_from_lead():
    lead = LeadFactory()
    TransferReservationFactory(lead=lead, rate=Decimal("500"))
    plan = PaymentPlanFactory(lead=lead, quote_total=Decimal("0"))
    plan.snapshot_total()
    assert plan.quote_total == Decimal("500.00")


# --- statuses --------------------------------------------------------------
def test_default_statuses():
    plan = PaymentPlanFactory()
    assert plan.deposit_status == PaymentPlan.DepositStatus.UNSENT
    assert plan.balance_status == PaymentPlan.BalanceStatus.NA


def test_is_paid_in_full_requires_both():
    plan = PaymentPlanFactory(
        deposit_status=PaymentPlan.DepositStatus.PAID,
        balance_status=PaymentPlan.BalanceStatus.PAID,
    )
    assert plan.is_paid_in_full is True
    plan.balance_status = PaymentPlan.BalanceStatus.SCHEDULED
    assert plan.is_paid_in_full is False


# --- balance schedule (30 days before earliest pickup) ---------------------
def test_balance_due_date_is_30_days_before_earliest_pickup():
    lead = LeadFactory()
    TransferReservationFactory(lead=lead, pickup_date=date.today() + timedelta(days=60))
    TransferReservationFactory(lead=lead, pickup_date=date.today() + timedelta(days=40))
    plan = PaymentPlanFactory(lead=lead)
    assert plan.balance_due_date == date.today() + timedelta(days=10)
    assert plan.balance_due_now is False


def test_balance_due_now_when_inside_window():
    lead = LeadFactory()
    TransferReservationFactory(lead=lead, pickup_date=date.today() + timedelta(days=20))
    plan = PaymentPlanFactory(lead=lead)
    assert plan.balance_due_now is True


def test_balance_due_date_none_without_pickups():
    plan = PaymentPlanFactory()
    assert plan.balance_due_date is None
    assert plan.balance_due_now is False


# --- charges ---------------------------------------------------------------
def test_record_charge_increments_attempt_and_unique_key():
    plan = PaymentPlanFactory()
    c1 = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("100"))
    c2 = plan.record_charge(kind=Charge.Kind.BALANCE, amount=Decimal("100"))
    assert c1.attempt_no == 1
    assert c2.attempt_no == 2
    assert c1.idempotency_key != c2.idempotency_key


def test_idempotency_key_is_unique():
    plan = PaymentPlanFactory()
    ChargeFactory(plan=plan, idempotency_key="dup")
    with pytest.raises(IntegrityError):
        ChargeFactory(plan=plan, idempotency_key="dup")
