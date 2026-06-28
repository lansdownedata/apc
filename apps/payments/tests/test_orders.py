import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db


def test_orders_requires_login(client):
    resp = client.get(reverse("orders_list"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_orders_lists_booked_only(client):
    booked = LeadFactory(status=Lead.Status.BOOKED)
    LeadFactory(status=Lead.Status.NEW)
    client.force_login(UserFactory())
    resp = client.get(reverse("orders_list"))
    assert resp.status_code == 200
    orders = list(resp.context["orders"])
    assert booked in [o["lead"] for o in orders]
    assert all(o["lead"].status == Lead.Status.BOOKED for o in orders)


def test_orders_finance_summary_totals(client):
    from decimal import Decimal

    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    lead = LeadFactory(status=Lead.Status.BOOKED)
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap-s",
    )
    client.force_login(UserFactory())
    summary = client.get(reverse("orders_list")).context["summary"]
    assert summary["deferred"] == Decimal("1000.00")
