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
        QboTerm.objects.create(realm_id="123", name="Net 30", qbo_id="3")
    group = AccountGroupFactory(qbo_id="55", invoice_email=email, terms="net_30" if terms else "")
    amounts = amounts or ["920.00", "184.00"]
    total = sum((Decimal(a) for a in amounts), Decimal("0"))
    invoice = InvoiceFactory(group=group, po_number=po, memo=memo, total=total)
    for amount in amounts:
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
    # An update carries the number QuickBooks gave it.
    invoice.qbo_id, invoice.qbo_realm_id, invoice.number = "900", "123", "1042"
    assert qbo_invoices.invoice_payload(invoice, "123")["DocNumber"] == "1042"
    # Linked in another company only: a create here, never that company's number.
    invoice.qbo_realm_id = "999"
    assert "DocNumber" not in qbo_invoices.invoice_payload(invoice, "123")


def test_payload_without_term_po_email_memo():
    body = qbo_invoices.invoice_payload(_invoice(terms=False, po="", memo="", email=""), "123")
    for key in ("SalesTermRef", "BillEmail", "PrivateNote", "DocNumber"):
        assert key not in body
    assert body["Line"][0]["Description"] == "Trip 920.00"


def test_update_payload_clears_the_note_when_po_and_memo_are_blank():
    invoice = _invoice(po="", memo="")
    invoice.qbo_id, invoice.qbo_realm_id, invoice.number = "900", "123", "1042"
    # Sparse: a key left out keeps what QuickBooks has, so a cleared note is sent blank.
    body = qbo_invoices.invoice_payload(invoice, "123")
    assert body["PrivateNote"] == ""
    assert body["DocNumber"] == "1042"


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


def test_payload_refuses_an_invoice_without_lines():
    from apps.payments.qbo_bills import _Refused

    invoice = _invoice()
    invoice.lines.all().delete()
    with pytest.raises(_Refused) as err:
        qbo_invoices.invoice_payload(invoice, "123")
    assert err.value.alert is False
    assert str(err.value) == qbo_invoices.NO_LINES


def test_payload_refuses_lines_that_dont_add_up_to_the_total():
    from apps.payments.qbo_bills import _Refused

    invoice = _invoice()
    Invoice.objects.filter(pk=invoice.pk).update(total=Decimal("1104.01"))
    invoice.refresh_from_db()
    with pytest.raises(_Refused) as err:
        qbo_invoices.invoice_payload(invoice, "123")
    assert err.value.alert is False
    assert str(err.value) == "This invoice's lines don't add up to its total."


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
    """What QuickBooks answers a create with: our item lines plus its own subtotal line, and
    the invoice's date and note as they stand now."""
    lines = [
        {"DetailType": "SalesItemLineDetail", "Amount": float(line.amount)}
        for line in invoice.lines.order_by("pk")
    ]
    total = sum(line["Amount"] for line in lines)
    answer = {
        "Id": qbo_id,
        "SyncToken": token,
        "DocNumber": doc,
        "TxnDate": invoice.invoice_date.isoformat(),
        "TotalAmt": total,
        "Line": [*lines, {"DetailType": "SubTotalLineDetail", "Amount": total}],
    }
    po = f"PO {invoice.po_number}" if invoice.po_number else ""
    note = " · ".join(part for part in (po, invoice.memo) if part)
    if note:
        answer["PrivateNote"] = note
    return {"Invoice": answer}


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
    # The date it waited on is set and it falls before it: not sent, and nothing to say.
    assert (early.qbo_sync_state, early.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert _alerts().count() == 2


def test_block_cleared_then_real_refusal_alerts(mapping):
    invoice = _open_invoice()
    mapping.customer_docs_from = None
    mapping.save()
    with patch(REQ) as req:
        qbo_invoices.sync_pending()
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_error == qbo_invoices.NO_START_DATE
    assert _alerts().count() == 1

    mapping.customer_docs_from = START
    mapping.save()
    refusal = _fault("6000", "A business validation error has occurred", detail="Inactive")
    with patch(REQ, side_effect=[refusal]):
        assert qbo_invoices.sync_pending() == 0
    invoice.refresh_from_db()
    assert invoice.qbo_sync_error == "QuickBooks didn't accept this invoice: Inactive"
    assert _alerts().count() == 2
    assert _alerts().order_by("pk").last().title == (
        f"QuickBooks invoice failed: {invoice.label}: {invoice.group}"
    )


def test_retry_clears_a_block_whose_setting_arrived(mapping):
    invoice = _open_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error=qbo_invoices.NO_TRIP_ITEM
    )
    refusal = _fault("6000", "A business validation error has occurred", detail="Inactive")
    with patch(REQ, side_effect=[refusal]):
        assert qbo_invoices.push_invoice(invoice) is False
    invoice.refresh_from_db()
    assert invoice.qbo_sync_error == "QuickBooks didn't accept this invoice: Inactive"
    assert _alerts().count() == 1  # its first real refusal


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
    assert _alerts().get().title == f"QuickBooks invoice failed: {invoice.label}: {invoice.group}"


def _taxed(invoice, total):
    """QuickBooks' answer when the item carries sales tax: same lines, a bigger TotalAmt."""
    answer = _sent(invoice)
    answer["Invoice"]["TotalAmt"] = total
    return answer


def test_mismatch_marks_error_with_both_totals_once(mapping):
    invoice = _open_invoice()
    with patch(REQ, return_value=_taxed(invoice, 1170.24)) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
    assert req.call_count == 1  # never rewritten on the next run
    invoice.refresh_from_db()
    assert invoice.qbo_id == "900" and invoice.number == "1042"
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == (
        "QuickBooks totals this invoice $1,170.24, not $1,104.00. "
        "Check sales tax on the Product/Service."
    )
    assert _alerts().count() == 1
    assert _alerts().get().detail == invoice.qbo_sync_error


def test_mismatched_line_count_marks_error(mapping):
    invoice = _open_invoice()
    answer = _sent(invoice)
    answer["Invoice"]["Line"].insert(0, {"DetailType": "SalesItemLineDetail", "Amount": 0})
    with patch(REQ, return_value=answer):
        assert qbo_invoices.push_invoice(invoice) is False
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == (
        "QuickBooks holds this invoice with 3 lines, not 2. Check it in QuickBooks."
    )
    assert _alerts().count() == 1


def test_hourly_never_updates_a_linked_open_invoice(mapping):
    invoice = _open_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(
        qbo_id="900",
        qbo_sync_token="0",
        qbo_realm_id="123",
        qbo_sync_state=SyncState.ERROR,
        qbo_sync_error="QuickBooks didn't accept this invoice: Inactive",
    )
    assert list(qbo_invoices.pending_invoices("123", START)) == []
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    # Linked in another company: pushed there as new.
    Invoice.objects.filter(pk=invoice.pk).update(qbo_realm_id="999")
    assert list(qbo_invoices.pending_invoices("123", START)) == [invoice]
    # A person's Retry still sends the linked one (an update).
    Invoice.objects.filter(pk=invoice.pk).update(qbo_realm_id="123")
    invoice.refresh_from_db()
    with patch(REQ, return_value=_sent(invoice, token="1")) as req:
        assert qbo_invoices.push_invoice(invoice) is True
    assert req.call_args.kwargs["json"]["Id"] == "900"


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


def test_run_stops_when_the_time_budget_runs_out(mapping):
    first = _open_invoice()
    _open_invoice()

    class AfterOnePush:
        def expired(self) -> bool:
            return req.call_count >= 1

    with patch(REQ, return_value=_sent(first)) as req:
        assert qbo_invoices.sync_pending(AfterOnePush()) == 1
    assert req.call_count == 1


def test_pending_invoices_put_unsent_ahead_of_refused(mapping):
    refused = _open_invoice()
    Invoice.objects.filter(pk=refused.pk).update(
        qbo_sync_state=SyncState.ERROR, qbo_sync_error="Inactive"
    )
    unsent = _open_invoice()
    pending = list(qbo_invoices.pending_invoices("123", START))
    assert [i.pk for i in pending] == [unsent.pk, refused.pk]


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
        params={"operation": "void", "requestid": f"apc-i{invoice.pk}-void-u0"},
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
    assert req.call_args_list[0].kwargs["params"]["requestid"] == f"apc-i{invoice.pk}-void-u0"
    assert req.call_args_list[1].args == ("GET", "invoice/900")
    assert req.call_args_list[2].kwargs["json"] == {"Id": "900", "SyncToken": "3"}
    # A new token is a new request: never the stale attempt's cached fault replayed.
    assert req.call_args_list[2].kwargs["params"]["requestid"] == f"apc-i{invoice.pk}-void-u3"
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
    assert _alerts().get().title == f"QuickBooks didn't void Invoice 1042: {invoice.group}"
    assert invoices.voids_waiting(invoice.group) == [invoice]


def test_voids_waiting_lists_pending_and_refused_voids_only():
    group = AccountGroupFactory()
    now = timezone.now()
    refused = InvoiceFactory(
        group=group,
        status=Invoice.Status.VOID,
        qbo_id="900",
        qbo_void_requested_at=now,
        qbo_sync_state=SyncState.ERROR,
    )
    queued = InvoiceFactory(
        group=group, status=Invoice.Status.VOID, qbo_id="901", qbo_void_requested_at=now
    )
    InvoiceFactory(  # voided in QuickBooks: done
        group=group,
        status=Invoice.Status.VOID,
        qbo_id="902",
        qbo_void_requested_at=now,
        qbo_sync_state=SyncState.SYNCED,
    )
    InvoiceFactory(group=group, status=Invoice.Status.VOID)  # never in QuickBooks
    InvoiceFactory(  # its create never landed: nothing to void there
        group=group, status=Invoice.Status.VOID, qbo_void_requested_at=now
    )
    InvoiceFactory(  # another group's
        status=Invoice.Status.VOID, qbo_id="903", qbo_void_requested_at=now
    )
    assert set(invoices.voids_waiting(group)) == {refused, queued}


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


def test_create_returning_after_a_void_leaves_the_invoice_in_pending_voids(mapping):
    invoice = _open_invoice()
    response = _sent(invoice)

    def void_then_answer(*args, **kwargs):
        # Staff void the invoice while QuickBooks is still making it.
        invoices.void_invoice(Invoice.objects.get(pk=invoice.pk), reason="Wrong PO", user=None)
        return response

    with patch(REQ, side_effect=void_then_answer):
        assert qbo_invoices.push_invoice(invoice) is False
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID
    assert invoice.qbo_id == "900"
    assert invoice.qbo_sync_state != SyncState.SYNCED
    assert list(qbo_invoices.pending_voids("123")) == [invoice]


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
    state = qbo_invoices.sync_state(invoice, connected=True)
    assert state == {
        "state": SyncState.ERROR,
        "error": "Nope",
        "sync_url": f"/portal/billing/invoices/{invoice.pk}/sync/",
        "before_start": False,
        "changed": False,
    }


def test_sync_state_says_a_sent_invoice_changed():
    invoice = InvoiceFactory(qbo_id="900", qbo_realm_id="123")
    assert qbo_invoices.sync_state(invoice, connected=True)["changed"] is True
    invoice.qbo_sync_state = SyncState.SYNCED
    assert qbo_invoices.sync_state(invoice, connected=True)["changed"] is False
    assert qbo_invoices.sync_state(InvoiceFactory(), connected=True)["changed"] is False


def test_sync_state_before_the_start_date(mapping):
    early = InvoiceFactory(invoice_date=START - timedelta(days=1))
    on_time = InvoiceFactory(invoice_date=START)
    assert qbo_invoices.sync_state(early, connected=True)["before_start"] is True
    assert qbo_invoices.sync_state(on_time, connected=True)["before_start"] is False
    assert qbo_invoices.sync_state(early, connected=True, start=None)["before_start"] is False


# --- APC-109: the books-closing date and the update of an edited invoice ---


def _prefs(close=None):
    info = {} if close is None else {"BookCloseDate": close}
    return {"Preferences": {"AccountingInfoPrefs": info}}


def _close_books(mapping, through):
    mapping.book_close_date = through
    mapping.save()


def test_refresh_preferences_reads_book_close_date():
    with patch(REQ, return_value=_prefs("2026-10-31")) as req:
        assert qbo_invoices.refresh_preferences() == date(2026, 10, 31)
    req.assert_called_once_with("GET", "preferences")
    assert QboAccountMapping.load().book_close_date == date(2026, 10, 31)
    malformed = (
        _prefs("31/10/2026"),
        _prefs("2026-02-30"),
        _prefs(20261031),
        {"Preferences": []},
        {"Preferences": {"AccountingInfoPrefs": "2026-10-31"}},
    )
    absent = (_prefs(), {}, {"Preferences": None}, {"Preferences": {"AccountingInfoPrefs": None}})
    for answer in (*absent, *malformed):
        QboAccountMapping.objects.filter(pk=1).update(book_close_date=date(2026, 10, 31))
        with patch(REQ, return_value=answer):
            assert qbo_invoices.refresh_preferences() is None
        assert QboAccountMapping.load().book_close_date is None


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"Preferences": None},
        {"Preferences": []},
        {"Preferences": {"AccountingInfoPrefs": None}},
        {"Preferences": {"AccountingInfoPrefs": "2026-10-31"}},
        _prefs("31/10/2026"),
        _prefs("2026-02-30"),
        _prefs(20261031),
    ],
)
def test_refresh_preferences_warns_on_a_missing_or_malformed_answer(answer, caplog):
    with patch(REQ, return_value=answer), caplog.at_level("WARNING", logger=qbo_invoices.__name__):
        assert qbo_invoices.refresh_preferences() is None
    assert any("preferences" in r.getMessage() for r in caplog.records)


def test_refresh_preferences_is_quiet_when_no_closing_date_is_set(caplog):
    with patch(REQ, return_value=_prefs()), caplog.at_level("WARNING"):
        assert qbo_invoices.refresh_preferences() is None
    assert caplog.records == []


def _edit(invoice, **kw):
    args = {"invoice_date": invoice.invoice_date, "po_number": "", "memo": "", "user": None}
    args.update(kw)
    return invoices.edit_invoice(Invoice.objects.get(pk=invoice.pk), **args)


def test_edited_invoice_is_pushed_as_sparse_update_and_stamp_cleared(mapping):
    invoice = _pushed(_open_invoice())
    assert invoice.qbo_txn_date == date(2026, 3, 1)
    _edit(invoice, invoice_date=date(2026, 3, 5), memo="Fixed")
    invoice.refresh_from_db()
    with patch(REQ, return_value=_sent(invoice, token="1")) as req:
        assert qbo_invoices.sync_pending() == 1
    req.assert_called_once()
    body = req.call_args.kwargs["json"]
    assert req.call_args.args == ("POST", "invoice")
    assert req.call_args.kwargs["params"] == {
        "requestid": f"apc-{qbo_bills._digest(body)}-i{invoice.pk}-u0"[:50]
    }
    assert (body["Id"], body["SyncToken"]) == ("900", "0")
    assert body["sparse"] is True
    assert body["DocNumber"] == "1042"
    assert body["TxnDate"] == "2026-03-05"
    assert body["PrivateNote"] == "Fixed"
    assert len(body["Line"]) == 2
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.SYNCED
    assert invoice.qbo_sync_token == "1"
    assert invoice.qbo_edit_requested_at is None
    assert invoice.qbo_txn_date == date(2026, 3, 5)
    assert list(qbo_invoices.pending_invoices("123", START)) == []


def test_mismatched_update_is_not_resent_hourly(mapping):
    invoice = _pushed(_open_invoice())
    _edit(invoice, memo="Fixed")
    invoice.refresh_from_db()
    with patch(REQ, return_value=_taxed(invoice, 1170.24)) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
    assert req.call_count == 1
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error.startswith("QuickBooks totals this invoice $1,170.24")
    assert invoice.qbo_edit_requested_at is None
    assert _alerts().count() == 1


def test_update_refused_when_the_date_quickbooks_holds_is_closed(mapping):
    invoice = _pushed(_open_invoice(invoice_date=date(2026, 10, 30)))
    _edit(invoice, invoice_date=date(2026, 11, 2))
    _close_books(mapping, date(2026, 10, 31))
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == (
        "This edit didn't reach QuickBooks: the period is closed there."
    )
    # Nor can it be voided here: QuickBooks still holds it in October.
    with pytest.raises(invoices.InvoiceError, match="In a closed period"):
        invoices.void_invoice(invoice, reason="Wrong date", user=None)


@pytest.mark.parametrize("change", [{"invoice_date": date(2026, 3, 9)}, {"memo": "Fixed"}])
def test_replayed_create_with_old_values_is_followed_by_an_update(mapping, change):
    invoice = _open_invoice()
    replay = _sent(invoice)  # what QuickBooks made from an attempt whose answer was lost
    _edit(invoice, **change)
    invoice.refresh_from_db()
    with patch(REQ, return_value=replay):
        assert qbo_invoices.push_invoice(invoice) is False
    invoice.refresh_from_db()
    assert (invoice.qbo_id, invoice.number) == ("900", "1042")
    assert (invoice.qbo_sync_state, invoice.qbo_sync_error) == (SyncState.NOT_SYNCED, "")
    assert invoice.qbo_edit_requested_at is not None
    assert invoice.qbo_txn_date == date(2026, 3, 1)
    with patch(REQ, return_value=_sent(invoice, token="1")) as req:
        assert qbo_invoices.sync_pending() == 1
    body = req.call_args.kwargs["json"]
    assert body["Id"] == "900"
    assert body["TxnDate"] == invoice.invoice_date.isoformat()
    assert body.get("PrivateNote", "") == invoice.memo
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.SYNCED
    assert invoice.qbo_edit_requested_at is None


def test_edit_during_create_is_pushed_as_an_update_afterwards(mapping):
    invoice = _open_invoice()
    response = _sent(invoice)

    def edit_then_answer(*args, **kwargs):
        # Staff correct the date while QuickBooks is still making the invoice.
        _edit(invoice, invoice_date=date(2026, 3, 9))
        return response

    with patch(REQ, side_effect=edit_then_answer):
        assert qbo_invoices.push_invoice(invoice) is False
    invoice.refresh_from_db()
    assert (invoice.qbo_id, invoice.number) == ("900", "1042")
    assert invoice.qbo_sync_state == SyncState.NOT_SYNCED
    assert invoice.qbo_edit_requested_at is not None
    assert list(qbo_invoices.pending_invoices("123", START)) == [invoice]
    with patch(REQ, return_value=_sent(invoice, token="1")) as req:
        assert qbo_invoices.sync_pending() == 1
    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["SyncToken"], body["TxnDate"]) == ("900", "0", "2026-03-09")
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.SYNCED
    assert invoice.qbo_edit_requested_at is None


def test_queued_edit_refused_when_period_closes(mapping):
    invoice = _pushed(_open_invoice())
    _edit(invoice, invoice_date=date(2026, 3, 5))
    _close_books(mapping, date(2026, 3, 31))
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.push_invoice(invoice) is False
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == qbo_invoices.CLOSED_UPDATE_MESSAGE
    assert invoice.qbo_edit_requested_at is not None
    assert _alerts().count() == 1
    assert _alerts().get().detail == qbo_invoices.CLOSED_UPDATE_MESSAGE


def test_edit_of_an_invoice_pushed_before_0011_is_gated_on_its_old_date(mapping):
    invoice = _pushed(_open_invoice())  # QuickBooks holds 2026-03-01
    Invoice.objects.filter(pk=invoice.pk).update(qbo_txn_date=None)  # as before migration 0011
    _edit(invoice, invoice_date=date(2026, 4, 5))
    _close_books(mapping, date(2026, 3, 31))  # March closed before the hourly run
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == qbo_invoices.CLOSED_UPDATE_MESSAGE


def test_edit_while_disconnected_is_sent_after_reconnecting(mapping):
    invoice = _pushed(_open_invoice())
    with patch("apps.billing.qbo_sync.current_realm", return_value=""):
        _edit(invoice, invoice_date=date(2026, 3, 5), memo="Fixed")
    invoice.refresh_from_db()
    assert list(qbo_invoices.pending_invoices("123", START)) == [invoice]
    with patch(REQ, return_value=_sent(invoice, token="1")) as req:
        assert qbo_invoices.sync_pending() == 1
    body = req.call_args.kwargs["json"]
    assert (body["Id"], body["TxnDate"], body["PrivateNote"]) == ("900", "2026-03-05", "Fixed")


def test_create_is_never_gated_by_the_closing_date(mapping):
    invoice = _open_invoice()
    _close_books(mapping, date(2026, 3, 31))
    with patch(REQ, return_value=_sent(invoice)) as req:
        assert qbo_invoices.sync_pending() == 1
    assert "Id" not in req.call_args.kwargs["json"]


def test_void_push_refused_in_closed_period(mapping):
    invoice = _pushed(_open_invoice())
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    _close_books(mapping, date(2026, 3, 1))
    with patch(REQ) as req:
        assert qbo_invoices.sync_pending() == 0
        assert qbo_invoices.push_void(invoice) is False
    req.assert_not_called()
    invoice.refresh_from_db()
    assert invoice.qbo_sync_state == SyncState.ERROR
    assert invoice.qbo_sync_error == invoices.CLOSED_PERIOD_MESSAGE
    assert _alerts().count() == 1
    assert _alerts().get().title == f"QuickBooks didn't void Invoice 1042: {invoice.group}"


def test_run_hourly_refreshes_preferences_and_survives_its_failure(mapping):
    invoice = _open_invoice()
    passes = (
        patch.object(qbo_bills, "sync_pending", return_value=0),
        patch("apps.billing.qbo_sync.run_hourly", return_value=0),
        patch.object(qbo_bills, "refresh_accounts"),
        patch.object(qbo_invoices, "refresh_items"),
    )
    with passes[0], passes[1], passes[2], passes[3]:
        with patch(REQ, side_effect=[_prefs("2026-01-31"), _sent(invoice)]) as req:
            assert qbo_bills.run_hourly() == 1
        assert req.call_args_list[0].args == ("GET", "preferences")
        assert QboAccountMapping.load().book_close_date == date(2026, 1, 31)
        # A failed read keeps the cached date and the push still runs.
        other = _open_invoice()
        down = qbo.QboAPIError(503, "Service Unavailable")
        with patch(REQ, side_effect=[down, _sent(other, qbo_id="901", doc="1043")]):
            assert qbo_bills.run_hourly() == 1
    assert QboAccountMapping.load().book_close_date == date(2026, 1, 31)
    other.refresh_from_db()
    assert other.qbo_sync_state == SyncState.SYNCED
