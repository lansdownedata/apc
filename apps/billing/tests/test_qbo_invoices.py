"""APC-107 — QuickBooks invoices: items cache, payload, push."""

import pytest
from django.db import IntegrityError, transaction

from apps.billing.factories import InvoiceFactory, QboItemFactory
from apps.billing.models import QboAccountMapping

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
