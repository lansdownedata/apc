"""Invoice and InvoiceLine (APC-106): a trip is on at most one invoice, and a group that
has been invoiced cannot be removed."""

import pytest
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from apps.billing import services
from apps.billing.factories import AccountGroupFactory, InvoiceFactory, InvoiceLineFactory

pytestmark = pytest.mark.django_db


def test_a_trip_is_on_at_most_one_invoice() -> None:
    first = InvoiceLineFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        InvoiceLineFactory(reservation=first.reservation)


def test_group_with_invoices_cannot_be_deleted() -> None:
    group = AccountGroupFactory(is_default=False)
    AccountGroupFactory(account=group.account, is_default=True)
    InvoiceFactory(group=group)
    with pytest.raises(services.BillingError, match="invoices"):
        services.delete_group(group)
    with pytest.raises(ProtectedError):
        group.delete()


def test_invoice_label() -> None:
    draft = InvoiceFactory(number="")
    assert draft.label == f"Invoice (draft #{draft.pk})"
    assert InvoiceFactory(number="1042").label == "Invoice 1042"
