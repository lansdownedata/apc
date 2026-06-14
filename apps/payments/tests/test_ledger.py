from decimal import Decimal

import pytest

from apps.core.choices import Account
from apps.leads.factories import LeadFactory
from apps.payments.models import JournalEntry, JournalLine

pytestmark = pytest.mark.django_db


def test_journal_entry_balances():
    lead = LeadFactory()
    entry = JournalEntry.objects.create(
        lead=lead, kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="t1"
    )
    JournalLine.objects.create(entry=entry, account=Account.CASH, debit=Decimal("100.00"))
    JournalLine.objects.create(
        entry=entry, account=Account.CUSTOMER_DEPOSITS, credit=Decimal("100.00")
    )
    assert entry.total_debit == Decimal("100.00")
    assert entry.total_credit == Decimal("100.00")
    assert entry.is_balanced is True
