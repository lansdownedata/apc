from datetime import date, timedelta
from unittest.mock import patch

import pytest

from apps.leads.factories import LeadFactory
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.payments.tasks import charge_due_balances
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def test_charges_only_due_scheduled_balances():
    # Due: scheduled + earliest pickup within 30 days.
    due_lead = LeadFactory()
    TransferReservationFactory(lead=due_lead, pickup_date=date.today() + timedelta(days=10))
    due_plan = PaymentPlanFactory(
        lead=due_lead,
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
        stripe_payment_method_id="pm_123",
    )

    # Not due: scheduled but pickup far out.
    future_lead = LeadFactory()
    TransferReservationFactory(lead=future_lead, pickup_date=date.today() + timedelta(days=90))
    PaymentPlanFactory(lead=future_lead, balance_status=PaymentPlan.BalanceStatus.SCHEDULED)

    with patch("apps.payments.tasks.services.charge_balance") as charge_balance:
        charged = charge_due_balances()

    charge_balance.assert_called_once_with(due_plan)
    assert charged == 1


def test_due_balances_without_a_saved_card_are_left_for_the_report():
    """A directly booked order may have no card; charging would only ever fail and raise a
    'balance failed' alert every day. It stays SCHEDULED and shows on the deposit report."""
    carded = LeadFactory()
    TransferReservationFactory(lead=carded, pickup_date=date.today() + timedelta(days=10))
    carded_plan = PaymentPlanFactory(
        lead=carded,
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
        stripe_payment_method_id="pm_123",
    )
    cardless = LeadFactory()
    TransferReservationFactory(lead=cardless, pickup_date=date.today() + timedelta(days=10))
    PaymentPlanFactory(lead=cardless, balance_status=PaymentPlan.BalanceStatus.SCHEDULED)

    with patch("apps.payments.tasks.services.charge_balance") as charge_balance:
        charged = charge_due_balances()

    charge_balance.assert_called_once_with(carded_plan)
    assert charged == 1
