"""Invoice corrections (APC-109): edit an open invoice's date, PO and memo; the books-closing
date that refuses edits to a closed period."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import invoices
from apps.billing.factories import AccountGroupFactory, InvoiceFactory, InvoiceLineFactory
from apps.billing.models import Invoice, QboAccountMapping, SyncState

pytestmark = pytest.mark.django_db

REALM = "apps.billing.qbo_sync.current_realm"


def _invoice(**kw) -> Invoice:
    group = kw.pop("group", None) or AccountGroupFactory(terms="net_30")
    return InvoiceFactory(
        group=group,
        invoice_date=date(2026, 11, 1),
        due_date=date(2026, 12, 1),
        po_number="4471",
        memo="Nov",
        total="100.00",
        **kw,
    )


def _edit(invoice: Invoice, **kw) -> Invoice:
    args = {
        "invoice_date": invoice.invoice_date,
        "po_number": invoice.po_number,
        "memo": invoice.memo,
        "user": UserFactory(),
    }
    args.update(kw)
    return invoices.edit_invoice(invoice, **args)


def _close_books(through: date) -> None:
    QboAccountMapping.objects.update_or_create(pk=1, defaults={"book_close_date": through})


def test_in_closed_period() -> None:
    assert invoices.in_closed_period(date(2026, 1, 1)) is False
    assert invoices.in_closed_period(None) is False
    _close_books(date(2026, 10, 31))
    assert invoices.in_closed_period(date(2026, 10, 31)) is True
    assert invoices.in_closed_period(date(2026, 10, 1)) is True
    assert invoices.in_closed_period(date(2026, 11, 1)) is False
    assert invoices.in_closed_period(None) is False


def test_edit_changes_date_po_memo_and_due_date() -> None:
    invoice = _invoice()
    user = UserFactory()
    edited = _edit(
        invoice, invoice_date=date(2026, 11, 5), po_number=" 4480 ", memo=" Nov fix ", user=user
    )
    edited.refresh_from_db()
    assert edited.invoice_date == date(2026, 11, 5)
    assert edited.due_date == date(2026, 12, 5)
    assert edited.po_number == "4480"
    assert edited.memo == "Nov fix"
    assert edited.edited_by == user
    assert edited.edited_at is not None
    assert edited.edit_note == "date 2026-11-01 → 2026-11-05 · PO 4471 → 4480 · memo changed"


def test_edit_never_touches_lines_or_total() -> None:
    invoice = _invoice()
    line = InvoiceLineFactory(invoice=invoice, amount="100.00", description="keep me")
    _edit(invoice, invoice_date=date(2026, 11, 9), memo="changed")
    invoice.refresh_from_db()
    line.refresh_from_db()
    assert str(invoice.total) == "100.00"
    assert invoice.lines.count() == 1
    assert (line.description, str(line.amount)) == ("keep me", "100.00")


def test_noop_edit_changes_nothing() -> None:
    invoice = _invoice(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    with patch(REALM, return_value="123"):
        _edit(invoice)
    invoice.refresh_from_db()
    assert invoice.edited_at is None and invoice.edited_by is None
    assert invoice.edit_note == "" and invoice.qbo_edit_requested_at is None
    assert invoice.qbo_sync_state == SyncState.SYNCED


def test_edit_respects_po_required() -> None:
    group = AccountGroupFactory(terms="net_30", po_required=True)
    invoice = _invoice(group=group)
    with pytest.raises(invoices.InvoiceError, match="This group needs a PO number."):
        _edit(invoice, po_number="  ")


def test_edit_validates_date_bounds() -> None:
    invoice = _invoice()
    for bad in (date(2019, 12, 31), timezone.localdate() + timedelta(days=366)):
        with pytest.raises(invoices.InvoiceError, match="Enter a valid invoice date."):
            _edit(invoice, invoice_date=bad)


def test_edit_refused_in_closed_period() -> None:
    old = _invoice()
    _close_books(date(2026, 11, 3))
    with pytest.raises(invoices.InvoiceError, match="In a closed period"):
        _edit(old, memo="late fix")
    other = InvoiceFactory(
        group=AccountGroupFactory(terms="net_30"),
        invoice_date=date(2026, 11, 10),
        due_date=date(2026, 12, 10),
    )
    with pytest.raises(invoices.InvoiceError, match="In a closed period"):
        _edit(other, invoice_date=date(2026, 11, 2))
    other.refresh_from_db()
    assert other.edited_at is None
    assert _edit(other, invoice_date=date(2026, 11, 4)).invoice_date == date(2026, 11, 4)


def test_edit_of_linked_invoice_queues_update() -> None:
    invoice = _invoice(
        qbo_id="55",
        qbo_realm_id="123",
        qbo_sync_state=SyncState.ERROR,
        qbo_sync_error="boom",
    )
    with patch(REALM, return_value="123"):
        _edit(invoice, memo="new memo")
    invoice.refresh_from_db()
    assert invoice.qbo_edit_requested_at is not None
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED
    assert invoice.qbo_sync_error == ""


def test_edit_of_invoice_from_other_realm_does_not_queue() -> None:
    invoice = _invoice(qbo_id="55", qbo_realm_id="999", qbo_sync_state=SyncState.SYNCED)
    with patch(REALM, return_value="123"):
        _edit(invoice, memo="new memo")
    invoice.refresh_from_db()
    assert invoice.qbo_edit_requested_at is None
    assert invoice.qbo_sync_state == SyncState.SYNCED


def test_edit_refused_on_void_invoice() -> None:
    invoice = _invoice(status=Invoice.Status.VOID)
    with pytest.raises(invoices.InvoiceError, match="Only an open invoice can be edited."):
        _edit(invoice, memo="x")
