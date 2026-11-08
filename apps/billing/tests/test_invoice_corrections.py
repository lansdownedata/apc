"""Invoice corrections (APC-109): edit an open invoice's date, PO and memo; the books-closing
date that refuses edits to a closed period."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import invoices, qbo_invoices
from apps.billing.factories import (
    AccountGroupFactory,
    InvoiceFactory,
    InvoiceLineFactory,
    QboItemFactory,
)
from apps.billing.models import AccountGroup, Invoice, QboAccountMapping, SyncState
from apps.billing.tests._helpers import _processed_trip
from apps.billing.tests.test_qbo_invoices import _fault
from apps.notifications.models import Notification
from apps.reservations import processing

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
    assert edited.edit_note == (
        "date 2026-11-01 → 2026-11-05 · due 2026-12-01 → 2026-12-05 · PO 4471 → 4480 · memo changed"
    )


def test_edit_moves_the_due_date_only_with_the_invoice_date() -> None:
    invoice = _invoice()
    # The group's terms changed since the invoice was made: a PO fix leaves the due date be.
    AccountGroup.objects.filter(pk=invoice.group_id).update(terms="net_15")
    edited = _edit(Invoice.objects.get(pk=invoice.pk), po_number="4480")
    edited.refresh_from_db()
    assert edited.due_date == date(2026, 12, 1)
    assert edited.edit_note == "PO 4471 → 4480"
    edited = _edit(edited, invoice_date=date(2026, 11, 2))
    edited.refresh_from_db()
    assert edited.due_date == date(2026, 11, 17)
    assert edited.edit_note == "date 2026-11-01 → 2026-11-02 · due 2026-12-01 → 2026-11-17"


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


def test_edit_while_disconnected_is_queued_for_reconnect() -> None:
    # No company to compare (as `void_invoice`): one with a QuickBooks id counts as there.
    invoice = _invoice(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    with patch(REALM, return_value=""):
        _edit(invoice, memo="new memo")
    invoice.refresh_from_db()
    assert invoice.qbo_edit_requested_at is not None
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED


def test_edit_of_a_linked_invoice_with_no_held_date_holds_its_old_one() -> None:
    # Pushed before QuickBooks' date was kept (migration 0011): it holds the invoice's date.
    invoice = _invoice(qbo_id="55", qbo_realm_id="123", qbo_sync_state=SyncState.SYNCED)
    assert invoice.qbo_txn_date is None
    with patch(REALM, return_value="123"):
        _edit(invoice, invoice_date=date(2026, 11, 5))
    invoice.refresh_from_db()
    assert invoice.qbo_txn_date == date(2026, 11, 1)
    _close_books(date(2026, 11, 3))
    assert invoices.closed_in_quickbooks(invoice) is True
    # Never sent: nothing held there.
    unsent = _invoice()
    _close_books(date(2026, 10, 31))
    _edit(unsent, invoice_date=date(2026, 11, 5))
    unsent.refresh_from_db()
    assert unsent.qbo_txn_date is None


def test_backfill_sets_the_held_date_on_pushed_invoices() -> None:
    from importlib import import_module

    from django.apps import apps as django_apps

    backfill = import_module("apps.billing.migrations.0012_backfill_qbo_txn_date")
    pushed = _invoice(qbo_id="55", qbo_realm_id="123")
    held = _invoice(qbo_id="56", qbo_realm_id="123", qbo_txn_date=date(2026, 10, 30))
    unsent = _invoice()
    backfill.forwards(django_apps, None)
    for row in (pushed, held, unsent):
        row.refresh_from_db()
    assert pushed.qbo_txn_date == date(2026, 11, 1)
    assert held.qbo_txn_date == date(2026, 10, 30)
    assert unsent.qbo_txn_date is None


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


def test_local_void_refused_in_closed_period_only_when_linked() -> None:
    import re

    linked = _invoice(qbo_id="900", qbo_realm_id="123")
    unsent = _invoice()
    elsewhere = _invoice(qbo_id="901", qbo_realm_id="999")
    _close_books(date(2026, 11, 30))
    with patch(REALM, return_value="123"):
        with pytest.raises(invoices.InvoiceError, match=re.escape(invoices.CLOSED_PERIOD_MESSAGE)):
            invoices.void_invoice(linked, reason="Wrong PO", user=None)
        linked.refresh_from_db()
        assert linked.status == Invoice.Status.OPEN
        assert linked.qbo_void_requested_at is None
    # Disconnected (no company to compare): one with a QuickBooks id counts as there.
    with patch(REALM, return_value=""):
        with pytest.raises(invoices.InvoiceError, match=re.escape(invoices.CLOSED_PERIOD_MESSAGE)):
            invoices.void_invoice(linked, reason="Wrong PO", user=None)
    with patch(REALM, return_value="123"):
        # Never in this company's books: voided here, QuickBooks has nothing of it to close.
        for invoice in (unsent, elsewhere):
            assert invoices.void_invoice(invoice, reason="Wrong PO", user=None).status == (
                Invoice.Status.VOID
            )
        # Once the books are open again the linked one voids as before.
        _close_books(date(2026, 10, 31))
        assert invoices.void_invoice(linked, reason="Wrong PO", user=None).status == (
            Invoice.Status.VOID
        )


def test_linked_invoice_cannot_be_dated_before_the_start_date() -> None:
    import re

    QboAccountMapping.objects.update_or_create(
        pk=1, defaults={"customer_docs_from": date(2026, 11, 1)}
    )
    linked = _invoice(qbo_id="55", qbo_realm_id="123")
    unsent = _invoice()
    message = (
        "Invoices already in QuickBooks can't be dated before the QuickBooks start date "
        "(Nov 1, 2026)."
    )
    with patch(REALM, return_value="123"):
        with pytest.raises(invoices.InvoiceError, match=re.escape(message)):
            _edit(linked, invoice_date=date(2026, 10, 31))
        assert _edit(linked, invoice_date=date(2026, 11, 2)).invoice_date == date(2026, 11, 2)
        # Never sent: before the start date it simply stays out of QuickBooks.
        assert _edit(unsent, invoice_date=date(2026, 10, 31)).invoice_date == date(2026, 10, 31)


@pytest.fixture
def accountant():
    return UserFactory(can_manage_payments=True)


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _invoiced(accountant, *, group=None, **kw):
    """An open invoice over two real processed trips."""
    group = group or AccountGroupFactory(terms="net_30")
    trips = [_processed_trip(group, accountant=accountant) for _ in range(2)]
    invoice = invoices.create_invoice(
        group,
        [t.pk for t in trips],
        invoice_date=date(2026, 11, 1),
        po_number="4471",
        memo="Nov",
        user=accountant,
    )
    if kw:
        Invoice.objects.filter(pk=invoice.pk).update(**kw)
        invoice.refresh_from_db()
    return invoice, trips


def _replace(invoice, user, **kw):
    args = {"reason": "Wrong PO", "invoice_date": date(2026, 11, 5), "po_number": "", "memo": ""}
    args.update(kw)
    return invoices.void_and_replace(invoice, user=user, **args)


def test_replace_voids_and_recreates_linked_both_ways(accountant) -> None:
    old, trips = _invoiced(accountant, qbo_id="900", qbo_realm_id="123", number="1042")
    new = _replace(old, accountant)
    old.refresh_from_db()
    assert old.status == Invoice.Status.VOID
    assert old.void_reason == "Wrong PO"
    assert old.replaced_by == new
    assert not old.lines.exists()
    assert new.status == Invoice.Status.OPEN
    assert set(new.lines.values_list("reservation_id", flat=True)) == {t.pk for t in trips}
    assert new.invoice_date == date(2026, 11, 5)
    assert new.memo == "Replaces Invoice 1042"
    assert new.po_number == ""  # a blank PO clears it
    assert old.qbo_void_requested_at is not None


def test_replace_keeps_given_po_and_memo(accountant) -> None:
    old, _ = _invoiced(accountant)
    new = _replace(old, accountant, po_number="9", memo="Fixed")
    assert (new.po_number, new.memo) == ("9", "Fixed")


def test_replace_strips_the_po_as_entered(accountant) -> None:
    old, _ = _invoiced(accountant)
    assert _replace(old, accountant, po_number="  4480 ").po_number == "4480"


def test_replace_of_a_linked_invoice_cannot_be_dated_before_the_start_date(accountant) -> None:
    import re

    QboAccountMapping.objects.update_or_create(
        pk=1, defaults={"customer_docs_from": date(2026, 11, 1)}
    )
    message = (
        "Invoices already in QuickBooks can't be dated before the QuickBooks start date "
        "(Nov 1, 2026)."
    )
    linked, _ = _invoiced(accountant, qbo_id="900", qbo_realm_id="123")
    for realm in ("123", ""):  # connected to its company, or disconnected
        with (
            patch(REALM, return_value=realm),
            pytest.raises(invoices.InvoiceError, match=re.escape(message)),
        ):
            _replace(linked, accountant, invoice_date=date(2026, 10, 31))
        _still_open(linked)
    with patch(REALM, return_value="123"):
        assert _replace(linked, accountant, invoice_date=date(2026, 11, 1)).invoice_date == (
            date(2026, 11, 1)
        )
        # Never sent: the replacement simply stays out of QuickBooks.
        unsent, _ = _invoiced(accountant)
        assert _replace(unsent, accountant, invoice_date=date(2026, 10, 31)).invoice_date == (
            date(2026, 10, 31)
        )


def test_replace_refused_in_closed_period(accountant) -> None:
    old, _ = _invoiced(accountant)
    _close_books(date(2026, 11, 1))
    with pytest.raises(invoices.InvoiceError, match=invoices.CLOSED_PERIOD_MESSAGE):
        _replace(old, accountant)
    old.refresh_from_db()
    assert old.status == Invoice.Status.OPEN
    assert old.lines.count() == 2


def test_replace_refused_on_void_invoice(accountant) -> None:
    old, _ = _invoiced(accountant)
    invoices.void_invoice(old, reason="x", user=accountant)
    with pytest.raises(invoices.InvoiceError, match="open"):
        _replace(old, accountant)


def test_replace_rolls_back_when_create_refused(accountant) -> None:
    old, _ = _invoiced(accountant)
    before = Invoice.objects.count()
    with (
        patch("apps.billing.invoices.create_invoice", side_effect=invoices.InvoiceError("no")),
        pytest.raises(invoices.InvoiceError),
    ):
        _replace(old, accountant)
    old.refresh_from_db()
    assert old.status == Invoice.Status.OPEN
    assert old.lines.count() == 2
    assert old.replaced_by is None
    assert Invoice.objects.count() == before


def test_replace_survives_a_refused_quickbooks_void(accountant) -> None:
    group = AccountGroupFactory(terms="net_30", qbo_id="55", qbo_realm_id="123")
    old, _ = _invoiced(
        accountant, group=group, qbo_id="900", qbo_realm_id="123", qbo_sync_token="0", number="1042"
    )
    config = QboAccountMapping.load()
    config.trip_item = QboItemFactory(realm_id="123", qbo_id="9")
    config.customer_docs_from = date(2026, 1, 1)
    config.save()
    refusal = _fault("6000", "A business validation error has occurred", detail="It has a payment")
    with (
        patch("apps.billing.qbo_invoices.current_realm", return_value="123"),
        patch("apps.billing.qbo_sync.current_realm", return_value="123"),
        patch("apps.integrations.qbo.is_connected", return_value=True),
    ):
        new = _replace(old, accountant)
        old.refresh_from_db()
        with patch("apps.billing.qbo_invoices.qbo.request", side_effect=refusal):
            qbo_invoices.push_void(old)
        old.refresh_from_db()
        assert old.qbo_sync_state == SyncState.ERROR
        assert old.qbo_sync_error == "QuickBooks didn't accept this void: It has a payment"
        assert invoices.voids_waiting(group) == [old]
        assert Notification.objects.filter(kind=Notification.Kind.SYNC_FAILED).count() == 1
        new.refresh_from_db()
        assert new.status == Invoice.Status.OPEN
        assert new in list(qbo_invoices.pending_invoices("123", date(2026, 1, 1)))


def test_unlock_message_says_void_and_replace(accountant) -> None:
    old, trips = _invoiced(accountant)
    with pytest.raises(processing.ProcessingError) as err:
        processing.unlock(trips[0], user=accountant, reason="price check")
    assert str(err.value) == f"On {old.label}. Void and replace it to change the price."


def _still_open(old) -> None:
    old.refresh_from_db()
    assert old.status == Invoice.Status.OPEN
    assert old.lines.count() == 2
    assert old.replaced_by is None


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"invoice_date": date(2019, 12, 31)}, "Enter a valid invoice date."),
        (
            {"invoice_date": timezone.localdate() + timedelta(days=366)},
            "Enter a valid invoice date.",
        ),
        ({"po_number": "9" * 65}, "The PO number is too long (64 characters at most)."),
        ({"memo": "m" * 256}, "The memo is too long (255 characters at most)."),
    ],
)
def test_replace_validates_its_fields_like_edit(accountant, fields, message) -> None:
    import re

    old, _ = _invoiced(accountant)
    before = Invoice.objects.count()
    with pytest.raises(invoices.InvoiceError, match=re.escape(message)):
        _replace(old, accountant, **fields)
    _still_open(old)
    assert Invoice.objects.count() == before


def test_replace_needs_a_po_when_the_group_requires_one(accountant) -> None:
    group = AccountGroupFactory(terms="net_30", po_required=True)
    old, _ = _invoiced(accountant, group=group, po_number="")
    with pytest.raises(invoices.InvoiceError, match="This group needs a PO number."):
        _replace(old, accountant, po_number="  ")
    _still_open(old)
    # A blank clears the old invoice's PO, so the group's rule refuses it.
    old.po_number = "4471"
    old.save()
    with pytest.raises(invoices.InvoiceError, match="This group needs a PO number."):
        _replace(old, accountant, po_number="")
    _still_open(old)


def test_replace_refused_when_the_new_date_is_in_a_closed_period(accountant) -> None:
    old, _ = _invoiced(accountant, invoice_date=date(2026, 11, 10), due_date=date(2026, 12, 10))
    _close_books(date(2026, 11, 3))
    with pytest.raises(invoices.InvoiceError, match=invoices.CLOSED_PERIOD_MESSAGE):
        _replace(old, accountant, invoice_date=date(2026, 11, 2))
    _still_open(old)
    assert _replace(old, accountant, invoice_date=date(2026, 11, 4)).invoice_date == date(
        2026, 11, 4
    )


def test_replace_refused_when_quickbooks_holds_it_in_a_closed_period(accountant) -> None:
    # Edited here to Nov 10, but QuickBooks still has it on Nov 1, inside the closed period.
    old, _ = _invoiced(
        accountant,
        invoice_date=date(2026, 11, 10),
        qbo_txn_date=date(2026, 11, 1),
        qbo_id="900",
        qbo_realm_id="123",
    )
    _close_books(date(2026, 11, 3))
    with (
        patch(REALM, return_value="123"),
        pytest.raises(invoices.InvoiceError, match=invoices.CLOSED_PERIOD_MESSAGE),
    ):
        _replace(old, accountant, invoice_date=date(2026, 11, 12))
    _still_open(old)


def test_closed_in_quickbooks_takes_a_loaded_config() -> None:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    invoice = _invoice()
    _close_books(date(2026, 11, 3))
    config = QboAccountMapping.load()
    with CaptureQueriesContext(connection) as queries:
        assert invoices.closed_in_quickbooks(invoice, config) is True
    assert len(queries) == 0
    config.book_close_date = date(2026, 10, 31)
    assert invoices.closed_in_quickbooks(invoice, config) is False


def test_invoice_admin_keeps_corrections_and_quickbooks_dates_read_only(rf) -> None:
    from django.contrib.admin.sites import site

    request = rf.get("/")
    request.user = UserFactory(is_superuser=True, is_staff=True)
    readonly = set(site._registry[Invoice].get_readonly_fields(request, _invoice()))
    assert {
        "invoice_date",
        "po_number",
        "memo",
        "edited_at",
        "edited_by",
        "edit_note",
        "qbo_edit_requested_at",
        "qbo_txn_date",
    } <= readonly
    mapping = QboAccountMapping.load()
    assert "book_close_date" in site._registry[QboAccountMapping].get_readonly_fields(
        request, mapping
    )
