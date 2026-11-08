"""APC-108 — QuickBooks card customers, Sales Receipts and Refund Receipts."""

import pytest
from django.db import IntegrityError, transaction

from apps.billing.factories import CustomerLinkFactory
from apps.billing.models import QboAccountMapping, SyncState
from apps.payments.factories import ChargeFactory

pytestmark = pytest.mark.django_db


def test_customer_link_is_one_per_contact() -> None:
    link = CustomerLinkFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        CustomerLinkFactory(contact=link.contact)


def test_charge_has_quickbooks_fields() -> None:
    charge = ChargeFactory()
    assert charge.qbo_sync_state == SyncState.NOT_SYNCED
    assert charge.qbo_push_started_at is None


def test_mapping_has_deposit_account() -> None:
    assert QboAccountMapping.load().sales_deposit_account is None
