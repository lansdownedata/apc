"""APC-107 — QuickBooks invoices: items cache, payload, push."""

from datetime import date
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction

from apps.billing import qbo_invoices
from apps.billing.factories import InvoiceFactory, QboItemFactory
from apps.billing.models import QboAccountMapping, QboItem

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
    with patch(REALM, return_value="123"):
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
