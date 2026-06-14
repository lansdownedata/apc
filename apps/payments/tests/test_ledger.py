from decimal import Decimal

import pytest

from apps.core.choices import Account
from apps.leads.factories import LeadFactory
from apps.payments import ledger
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


def test_post_entry_creates_balanced_entry():
    lead = LeadFactory()
    entry = ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.ADJUSTMENT,
        lines=[
            (Account.CASH, Decimal("50.00"), Decimal("0.00")),
            (Account.CUSTOMER_DEPOSITS, Decimal("0.00"), Decimal("50.00")),
        ],
        idempotency_key="ok1",
    )
    assert entry.is_balanced
    assert entry.lines.count() == 2


def test_post_entry_rejects_unbalanced():
    lead = LeadFactory()
    with pytest.raises(ledger.LedgerError):
        ledger.post_entry(
            lead=lead,
            kind=JournalEntry.Kind.ADJUSTMENT,
            lines=[(Account.CASH, Decimal("10.00"), Decimal("0.00"))],
            idempotency_key="bad1",
        )


def test_post_entry_is_idempotent():
    lead = LeadFactory()
    lines = [
        (Account.CASH, Decimal("10.00"), Decimal("0.00")),
        (Account.CUSTOMER_DEPOSITS, Decimal("0.00"), Decimal("10.00")),
    ]
    first = ledger.post_entry(
        lead=lead, kind=JournalEntry.Kind.ADJUSTMENT, lines=lines, idempotency_key="dup"
    )
    second = ledger.post_entry(
        lead=lead, kind=JournalEntry.Kind.ADJUSTMENT, lines=lines, idempotency_key="dup"
    )
    assert first.pk == second.pk
    assert JournalEntry.objects.filter(idempotency_key="dup").count() == 1


def test_capture_credits_deferred():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap-dep",
    )
    bals = ledger.order_balances(lead)
    assert bals["collected"] == Decimal("1335.00")
    assert bals["deferred"] == Decimal("1335.00")
    assert bals["ar"] == Decimal("0.00")


def test_capture_clears_ar_before_deferred():
    lead = LeadFactory()
    # Manually create $165 of A/R (Dr A/R / Cr Recognized Revenue).
    ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.ADJUSTMENT,
        lines=[
            (Account.ACCOUNTS_RECEIVABLE, Decimal("165.00"), Decimal("0.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("165.00")),
        ],
        idempotency_key="seed-ar",
    )
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="cap-bal",
    )
    bals = ledger.order_balances(lead)
    assert bals["ar"] == Decimal("0.00")
    assert bals["deferred"] == Decimal("1170.00")  # 1335 − 165 cleared from A/R
