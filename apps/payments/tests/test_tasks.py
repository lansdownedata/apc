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
    due_plan = PaymentPlanFactory(lead=due_lead, balance_status=PaymentPlan.BalanceStatus.SCHEDULED)

    # Not due: scheduled but pickup far out.
    future_lead = LeadFactory()
    TransferReservationFactory(lead=future_lead, pickup_date=date.today() + timedelta(days=90))
    PaymentPlanFactory(lead=future_lead, balance_status=PaymentPlan.BalanceStatus.SCHEDULED)

    with patch("apps.payments.tasks.services.charge_balance") as charge_balance:
        charged = charge_due_balances()

    charge_balance.assert_called_once_with(due_plan)
    assert charged == 1
