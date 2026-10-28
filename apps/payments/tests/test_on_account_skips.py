"""APC-105: everything that assumes a card skips an order billed to an account (spec §5).

Each card path gets the same question: would it act on this order were it a card order?
Every test builds an order the path WOULD act on, then puts it on account.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import reports
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.payments.tasks import charge_due_balances
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


def on_account(lead: Lead) -> Lead:
    lead.billing_group = AccountGroupFactory(account__contact=lead.contact)
    lead.save(update_fields=["billing_group"])
    return lead


def _due_order(*, status=Lead.Status.BOOKED, days_out=10, **plan_kw):
    lead = LeadFactory(status=status)
    TransferReservationFactory(
        lead=lead,
        rate=Decimal("400.00"),
        pickup_date=timezone.localdate() + timedelta(days=days_out),
    )
    plan = PaymentPlanFactory(
        lead=lead,
        quote_total=Decimal("400.00"),
        balance_status=PaymentPlan.BalanceStatus.SCHEDULED,
        stripe_payment_method_id="pm_123",
        **plan_kw,
    )
    return lead, plan


def test_the_balance_cron_never_charges_an_account_order():
    card, card_plan = _due_order()
    on_account(_due_order()[0])

    with patch("apps.payments.tasks.services.charge_balance") as charge_balance:
        assert charge_due_balances() == 1

    charge_balance.assert_called_once_with(card_plan)


def test_the_unpaid_deposit_report_leaves_out_an_account_order():
    card, _ = _due_order()
    on_account(_due_order()[0])

    assert [r["lead"].pk for r in reports.unpaid_deposit_rows()] == [card.pk]
