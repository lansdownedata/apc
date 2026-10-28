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


def test_a_day_with_only_account_orders_sends_no_deposit_report(mailoutbox, settings):
    from apps.payments.tasks import send_unpaid_deposit_report

    settings.DEPOSIT_REPORT_EMAILS = ["office@example.com"]
    on_account(_due_order()[0])

    assert send_unpaid_deposit_report() == 0
    assert mailoutbox == []


def test_the_exceptions_dashboard_does_not_count_an_account_order():
    from apps.tasks import exceptions

    _due_order()
    on_account(_due_order()[0])

    assert exceptions.counts()["unpaid_deposits"] == 1


def test_reconcile_still_records_money_that_was_already_in_flight():
    """Decision 5: the switch cancels open intents, so this is a race the switch lost. If
    money did land, it is real and must reach the books."""
    from unittest.mock import MagicMock

    from apps.payments import tasks

    lead, plan = _due_order()
    on_account(lead)
    plan.charges.create(
        kind="balance",
        amount=Decimal("10.00"),
        status="pending",
        stripe_payment_intent_id="pi_late",
        idempotency_key="late",
    )
    plan.charges.update(updated_at=timezone.now() - timedelta(hours=1))

    with (
        patch("apps.payments.services._stripe") as stripe,
        patch("apps.payments.tasks.services.record_payment") as record,
    ):
        stripe.return_value.PaymentIntent.retrieve.return_value = MagicMock(status="succeeded")
        assert tasks.reconcile_open_charges() == 1
    record.assert_called_once()


def test_confirm_never_captures_a_hold_on_an_account_order():
    from apps.payments import services

    lead, _ = _due_order()
    on_account(lead)
    with (
        patch("apps.payments.services._stripe") as stripe,
        pytest.raises(services.PaymentError, match="billed to an account"),
    ):
        services.confirm_order(lead)
    stripe.assert_not_called()
