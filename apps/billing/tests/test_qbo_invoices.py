"""APC-107 — QuickBooks invoices: items cache, payload, push."""

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing import invoices, qbo_invoices
from apps.billing.factories import (
    AccountGroupFactory,
    InvoiceFactory,
    InvoiceLineFactory,
    QboItemFactory,
)
from apps.billing.models import Invoice, QboAccountMapping, QboItem, SyncState
from apps.billing.qbo_sync import SyncPaused
from apps.integrations import qbo
from apps.notifications.models import Notification
from apps.payments import qbo_bills

pytestmark = pytest.mark.django_db


def test_qbo_item_unique_per_realm():
    QboItemFactory(realm_id="123", qbo_id="7")
    with pytest.raises(IntegrityError), transaction.atomic():
        QboItemFactory(realm_id="123", qbo_id="7")


def test_mapping_has_customer_document_fields():
    mapping = QboAccountMapping.load()
    assert mapping.trip_item is None
    assert mapping.customer_docs_from is None


def test_invoice_void_stamp_defaults_null():
    assert InvoiceFactory().qbo_void_requested_at is None


REALM = "apps.billing.qbo_invoices.current_realm"
REQ = "apps.billing.qbo_invoices.qbo.request"


@pytest.fixture(autouse=True)
def realm():
    with (
        patch(REALM, return_value="123"),
        patch("apps.billing.qbo_sync.current_realm", return_value="123"),
        patch("apps.integrations.qbo.is_connected", return_value=True),
    ):
        yield


def _page(*rows):
    return {"QueryResponse": {"Item": list(rows)}}


def test_refresh_items_pages_and_retires():
    first = [{"Id": "1", "Name": "Trip", "Type": "Service", "Active": True}]
    second = [{"Id": "2", "Name": "Misc", "Type": "NonInventory"}]
    with patch(REQ, return_value=_page(*first, *second)) as req:
        assert qbo_invoices.refresh_items() == 2
    query = req.call_args.kwargs["params"]["query"]
    assert "Type in ('Service', 'NonInventory')" in query and "startposition 1" in query
    with patch(REQ, return_value=_page(*first)):
        assert qbo_invoices.refresh_items() == 1
    assert QboItem.objects.get(qbo_id="2").active is False
    assert QboItem.objects.get(qbo_id="1").item_type == "Service"


def test_refresh_items_reads_every_page():
    full = [{"Id": str(n), "Name": f"I{n}", "Type": "Service"} for n in range(1000)]
    with (
        patch.object(qbo_invoices, "ITEM_PAGE", 1000),
        patch(
            REQ, side_effect=[_page(*full), _page({"Id": "x", "Name": "Last", "Type": "Service"})]
        ) as req,
    ):
        assert qbo_invoices.refresh_items() == 1001
    assert "startposition 1001" in req.call_args.kwargs["params"]["query"]


def test_item_choices_only_active_in_realm():
    mine = QboItemFactory(realm_id="123")
    QboItemFactory(realm_id="123", active=False)
    QboItemFactory(realm_id="999")
    assert qbo_invoices.item_choices() == [(mine.pk, mine.name)]


def test_trip_item_ref_none_when_unset_or_inactive():
    assert qbo_invoices.trip_item_ref() is None
    item = QboItemFactory(realm_id="123", qbo_id="77")
    config = QboAccountMapping.load()
    config.trip_item = item
    config.save()
    assert qbo_invoices.trip_item_ref() == {"value": "77"}
    item.active = False
    item.save()
    assert qbo_invoices.trip_item_ref() is None


def test_customer_docs_from_reads_the_mapping():
    assert qbo_invoices.customer_docs_from() is None
    config = QboAccountMapping.load()
    config.customer_docs_from = date(2026, 12, 1)
    config.save()
    assert qbo_invoices.customer_docs_from() == date(2026, 12, 1)


# --- the Invoice payload ---


def _invoice(*, terms=True, po="4471", memo="Oct trips", email="ap@example.com", amounts=None):
    from apps.billing.factories import AccountGroupFactory, InvoiceLineFactory
    from apps.billing.models import QboTerm

    item = QboItemFactory(realm_id="123", qbo_id="9")
    config = QboAccountMapping.load()
    config.trip_item = item
    config.save()
    if terms:
        QboTerm.objects.create(name="Net 30", qbo_id="3")
    group = AccountGroupFactory(qbo_id="55", invoice_email=email, terms="net_30" if terms else "")
    invoice = InvoiceFactory(group=group, po_number=po, memo=memo)
    for amount in amounts or ["920.00", "184.00"]:
        InvoiceLineFactory(invoice=invoice, description=f"Trip {amount}", amount=amount)
    return invoice


def test_payload_shape_exact():
    invoice = _invoice()
    body = qbo_invoices.invoice_payload(invoice, "123")
    assert body == {
        "CustomerRef": {"value": "55"},
        "TxnDate": invoice.invoice_date.isoformat(),
        "DueDate": invoice.due_date.isoformat(),
        "SalesTermRef": {"value": "3"},
        "BillEmail": {"Address": "ap@example.com"},
        "PrivateNote": "PO 4471 · Oct trips",
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": "920.00",
                "Description": "PO 4471 · Trip 920.00",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "9"},
                    "Qty": 1,
                    "UnitPrice": "920.00",
                },
            },
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": "184.00",
                "Description": "Trip 184.00",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": "9"},
                    "Qty": 1,
                    "UnitPrice": "184.00",
                },
            },
        ],
    }
    assert "DocNumber" not in body


def test_payload_without_term_po_email_memo():
    body = qbo_invoices.invoice_payload(_invoice(terms=False, po="", memo="", email=""), "123")
    for key in ("SalesTermRef", "BillEmail", "PrivateNote", "DocNumber"):
        assert key not in body
    assert body["Line"][0]["Description"] == "Trip 920.00"


def test_payload_refuses_without_trip_item():
    from apps.payments.qbo_bills import _Refused

    invoice = _invoice()
    config = QboAccountMapping.load()
    config.trip_item = None
    config.save()
    with pytest.raises(_Refused) as err:
        qbo_invoices.invoice_payload(invoice, "123")
    assert err.value.alert is False
    assert str(err.value) == qbo_invoices.NO_TRIP_ITEM


def test_description_cut_to_4000():
    invoice = _invoice(po="", memo="")
    invoice.memo = "m" * 5000  # the column holds 255; the cut guards the wire anyway
    line = invoice.lines.first()
    line.description = "d" * 5000
    line.save()
    body = qbo_invoices.invoice_payload(invoice, "123")
    assert len(body["Line"][0]["Description"]) == 4000
    assert len(body["PrivateNote"]) == 4000


def test_matches_ignores_subtotal_line():
    body = qbo_invoices.invoice_payload(_invoice(), "123")
    item = {"DetailType": "SalesItemLineDetail"}
    sub = {"DetailType": "SubTotalLineDetail"}
    assert qbo_invoices.invoice_matches({"TotalAmt": 1104.0, "Line": [item, item, sub]}, body)
    assert not qbo_invoices.invoice_matches({"TotalAmt": 1104.01, "Line": [item, item, sub]}, body)
    assert not qbo_invoices.invoice_matches(
        {"TotalAmt": 1104.0, "Line": [item, item, item, sub]}, body
    )


# --- the push ---

START = date(2026, 1, 1)


@pytest.fixture
def mapping():
    config = QboAccountMapping.load()
    config.trip_item = QboItemFactory(realm_id="123", qbo_id="9")
    config.customer_docs_from = START
    config.save()
    return config


def _open_invoice(*, amounts=("920.00", "184.00"), invoice_date=date(2026, 3, 1), group=None):
    group = group or AccountGroupFactory(qbo_id="55", qbo_realm_id="123")
    invoice = InvoiceFactory(
        group=group,
        invoice_date=invoice_date,
        due_date=invoice_date + timedelta(days=30),
        total=sum((Decimal(a) for a in amounts), Decimal("0")),
    )
    for amount in amounts:
        InvoiceLineFactory(invoice=invoice, description=f"Trip {amount}", amount=amount)
    return invoice


def _sent(invoice, *, doc="1042", qbo_id="900", token="0"):
    """What QuickBooks answers a create with: our item lines plus its own subtotal line."""
    lines = [
        {"DetailType": "SalesItemLineDetail", "Amount": float(line.amount)}
        for line in invoice.lines.order_by("pk")
    ]
    total = sum(line["Amount"] for line in lines)
    return {
        "Invoice": {
            "Id": qbo_id,
            "SyncToken": token,
            "DocNumber": doc,
            "TotalAmt": total,
            "Line": [*lines, {"DetailType": "SubTotalLineDetail", "Amount": total}],
        }
    }


def _fault(code, message="", status=400, detail=""):
    error = {"code": code, "Message": message}
    if detail:
        error["Detail"] = detail
    return qbo.QboAPIError(status, json.dumps({"Fault": {"Error": [error]}}))


def _alerts():
    return Notification.objects.filter(kind=Notification.Kind.SYNC_FAILED)


def _pushed(invoice):
    with patch(REQ, return_value=_sent(invoice)):
        assert qbo_invoices.push_invoice(invoice) is True
    invoice.refresh_from_db()
    return invoice


def test_push_creates_and_stores_docnumber(mapping):
    invoice = _open_invoice()
    with patch(REQ, return_value=_sent(invoice)) as req:
        assert qbo_invoices.push_invoice(invoice) is True
    assert req.call_args.args == ("POST", "invoice")
    assert req.call_args.kwargs["params"] == {"requestid": f"apc-i{invoice.pk}-create"}
    assert "DocNumber" not in req.call_args.kwargs["json"]
    invoice.refresh_from_db()
    assert invoice.number == "1042"
    assert invoice.label == "Invoice 1042"
    assert invoice.qbo_id == "900"
    assert invoice.qbo_sync_token == "0"
    assert invoice.qbo_sync_state == SyncState.SYNCED
    assert invoice.qbo_realm_id == "123"
    assert invoice.qbo_synced_at is not None
    assert invoice.qbo_push_started_at is None  # the lease is released


def test_create_requestid_is_stable_until_quickbooks_refuses(mapping):
    invoice = _open_invoice()
    effects = [
        qbo.QboAPIError(0, "read timed out"),  # the response is lost
        _fault("6000", "A business validation error has occurred", detail="Bad customer"),
        _sent(invoice),
    ]
    with patch(REQ, side_effect=effects) as req:
        with pytest.raises(SyncPaused):
            qbo_invoices.push_invoice(invoice)
        assert qbo_invoices.push_invoice(invoice) is False
        invoice.refresh_from_db()
        assert invoice.qbo_create_attempt == 1
        assert qbo_invoices.push_invoice(invoice) is True
    ids = [call.kwargs["params"]["requestid"] for call in req.call_args_list]
    assert ids == [
        f"apc-i{invoice.pk}-create",
        f"apc-i{invoice.pk}-create",
        f"apc-i{invoice.pk}-create1",
    ]


def test_push_invoice_refuses_while_leased(mapping):
    invoice = _open_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(qbo_push_started_at=timezone.now())
    with patch(REQ) as req:
        assert qbo_invoices.push_invoice(invoice) is False
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()


def test_push_invoice_never_sends_a_void_invoice(mapping):
    invoice = _open_invoice()
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    with patch(REQ) as req:
        assert qbo_invoices.push_invoice(invoice) is False
    req.assert_not_called()


def test_invoice_waits_for_its_group(mapping):
    group = AccountGroupFactory(qbo_id="")
    invoice = _open_invoice(group=group)
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
        group.qbo_id, group.qbo_realm_id = "55", "999"  # in another company: still waits
        group.save()
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED
    assert invoice.qbo_sync_error == ""
    group.qbo_realm_id = "123"
    group.save()
    with patch(REQ, return_value=_sent(invoice)):
        assert qbo_invoices.sync_pending() == 1


def test_pending_window_and_blocks(mapping):
    early = _open_invoice(invoice_date=date(2025, 12, 31))
    invoice = _open_invoice()
    InvoiceFactory(group=invoice.group, invoice_date=date(2026, 3, 1))  # no lines: never sent
    assert list(qbo_invoices.pending_invoices("123", START)) == [invoice]

    mapping.customer_docs_from = None
    mapping.save()
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    for row in (early, invoice):
        row.refresh_from_db()
        assert row.qbo_sync_state == SyncState.ERROR
        assert row.qbo_sync_error == qbo_invoices.NO_START_DATE
    assert _alerts().count() == 1
    assert _alerts().get().title == "Customer invoices can't go to QuickBooks yet"

    mapping.customer_docs_from = START
    mapping.trip_item = None
    mapping.save()
    with patch(REQ) as req:
        qbo_invoices.sync_pending()
        qbo_invoices.sync_pending()
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_error == qbo_invoices.NO_TRIP_ITEM
    early.refresh_from_db()
    assert early.qbo_sync_error == qbo_invoices.NO_START_DATE  # outside the window: untouched
    assert _alerts().count() == 2


def test_start_date_change_leaves_sent_and_earlier_invoices_alone(mapping):
    sent = _pushed(_open_invoice(invoice_date=date(2026, 2, 1)))
    unsent = _open_invoice(invoice_date=date(2026, 2, 15))
    mapping.customer_docs_from = date(2026, 3, 1)
    mapping.save()
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    sent.refresh_from_db()
    unsent.refresh_from_db()
    assert (sent.qbo_sync_state, sent.number) == (SyncState.SYNCED, "1042")
    assert (unsent.qbo_sync_state, unsent.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert not _alerts().exists()


def test_refusal_marks_error_once(mapping):
    invoice = _open_invoice()
    refusal = _fault("6000", "A business validation error has occurred", detail="Inactive")
    with patch(REQ, side_effect=[refusal, refusal]) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
    assert req.call_count == 2
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error.startswith("QuickBooks didn't accept this invoice")
    assert invoice.qbo_sync_error == "QuickBooks didn't accept this invoice: Inactive"
    assert _alerts().count() == 1
    assert _alerts().get().title == f"QuickBooks invoice failed: {invoice.group}"


def test_outage_pauses_nothing_marked(mapping):
    invoice = _open_invoice()
    _open_invoice()
    with patch(REQ, side_effect=qbo.QboAPIError(503, "Service Unavailable")) as req:
        assert qbo_invoices.sync_pending() == 0
    assert req.call_count == 1  # the run stops at the outage
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED
    assert invoice.qbo_sync_error == ""
    assert invoice.qbo_id == ""
    assert invoice.qbo_create_attempt == 0
    assert invoice.qbo_push_started_at is None
    assert not _alerts().exists()


def test_run_stops_at_the_batch(mapping):
    first = _open_invoice()
    _open_invoice()
    with (
        patch.object(qbo_invoices, "SYNC_BATCH", 1),
        patch(REQ, return_value=_sent(first)) as req,
    ):
        assert qbo_invoices.sync_pending() == 1
    assert req.call_count == 1


def test_void_pushes_operation_void(mapping):
    invoice = _pushed(_open_invoice())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID
    assert invoice.qbo_void_requested_at is not None
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED
    assert not invoice.lines.exists()
    assert list(qbo_invoices.pending_voids("123")) == [invoice]
    assert list(qbo_invoices.pending_invoices("123", START)) == []
    voided = {"Invoice": {"Id": "900", "SyncToken": "1", "TotalAmt": 0}}
    with patch(REQ, return_value=voided) as req:
        assert qbo_invoices.sync_pending() == 1
    req.assert_called_once_with(
        "POST",
        "invoice",
        params={"operation": "void", "requestid": f"apc-i{invoice.pk}-void"},
        json={"Id": "900", "SyncToken": "0"},
    )
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.SYNCED
    assert invoice.qbo_sync_token == "1"
    assert invoice.qbo_push_started_at is None
    assert list(qbo_invoices.pending_voids("123")) == []


def test_void_takes_the_current_sync_token_when_quickbooks_changed_it(mapping):
    invoice = _pushed(_open_invoice())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    effects = [
        _fault("5010", "Stale Object Error"),
        {"Invoice": {"Id": "900", "SyncToken": "3"}},
        {"Invoice": {"Id": "900", "SyncToken": "4"}},
    ]
    with patch(REQ, side_effect=effects) as req:
        assert qbo_invoices.push_void(invoice) is True
    assert req.call_args_list[1].args == ("GET", "invoice/900")
    assert req.call_args_list[2].kwargs["json"] == {"Id": "900", "SyncToken": "3"}
    invoice.refresh_from_db()
    assert (invoice.qbo_sync_state, invoice.qbo_sync_token) == (SyncState.SYNCED, "4")


def test_void_refusal_marks_error_once(mapping):
    invoice = _pushed(_open_invoice())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    refusal = _fault("6000", "A business validation error has occurred", detail="It has a payment")
    with patch(REQ, side_effect=[refusal, refusal]):
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID
    assert not invoice.lines.exists()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == "QuickBooks didn't accept this void: It has a payment"
    assert _alerts().count() == 1


def test_void_outage_pauses_nothing_marked(mapping):
    invoice = _pushed(_open_invoice())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    with patch(REQ, side_effect=qbo.QboAPIError(0, "timed out")):
        assert qbo_invoices.sync_pending() == 0
        with pytest.raises(SyncPaused):
            qbo_invoices.push_void(invoice)
    invoice.refresh_from_db()
    assert (invoice.qbo_sync_state, invoice.qbo_sync_error) == (SyncState.NOT_SYNCED, "")


def test_void_during_a_create_still_voids_in_quickbooks(mapping):
    invoice = _open_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(qbo_push_started_at=timezone.now())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    invoice.refresh_from_db()
    # The create in flight may land: the void is queued so it follows the invoice there.
    assert invoice.qbo_void_requested_at is not None


def test_run_hourly_pushes_invoices_last(mapping):
    invoice = _open_invoice()
    with (
        patch.object(qbo_bills, "sync_pending", return_value=0),
        patch("apps.billing.qbo_sync.run_hourly", return_value=0),
        patch.object(qbo_bills, "refresh_accounts"),
        patch.object(qbo_invoices, "refresh_items"),
        patch(REQ, return_value=_sent(invoice)),
    ):
        assert qbo_bills.run_hourly() == 1
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.SYNCED


def test_sync_state_none_when_disconnected():
    invoice = InvoiceFactory()
    assert qbo_invoices.sync_state(invoice, connected=False) is None
    with patch("apps.integrations.qbo.is_connected", return_value=False):
        assert qbo_invoices.sync_state(invoice) is None
        assert qbo_invoices.sync_pending() == 0


def test_sync_state_when_connected():
    invoice = InvoiceFactory(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Nope")
    with patch("django.urls.reverse", return_value="/sync/") as reverse:
        state = qbo_invoices.sync_state(invoice, connected=True)
    reverse.assert_called_once_with("billing_invoice_sync", args=[invoice.pk])
    assert state == {"state": SyncState.ERROR, "error": "Nope", "sync_url": "/sync/"}
