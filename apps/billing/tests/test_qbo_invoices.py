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
