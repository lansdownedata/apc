"""Pipeline kanban: grouping, column values, payment chips, render."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def test_columns_group_and_sum(logged_in_client):
    quoted = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=quoted, rate=Decimal("500"))
    LeadFactory(status=Lead.Status.NEW)
    resp = logged_in_client.get(reverse("pipeline"))
    columns = {c["status"]: c for c in resp.context["columns"]}
    assert [c["status"] for c in resp.context["columns"]] == ["new", "quoted", "booked", "lost"]
    assert len(columns["quoted"]["leads"]) == 1
    assert columns["quoted"]["value"] == Decimal("500")
    assert len(columns["new"]["leads"]) == 1


def test_payment_chip_states(logged_in_client):
    booked = LeadFactory(status=Lead.Status.BOOKED)
    PaymentPlanFactory(lead=booked, deposit_status="paid", balance_status="failed")
    resp = logged_in_client.get(reverse("pipeline"))
    assert "Balance failed" in resp.content.decode()


def test_page_renders_cards_and_open_value(logged_in_client):
    lead = LeadFactory(status=Lead.Status.NEW)
    resp = logged_in_client.get(reverse("pipeline"))
    html = resp.content.decode()
    assert "Pipeline" in html
    assert lead.contact.name in html
    assert "Open pipeline" in html


def test_payment_chip_property_states():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    assert lead.payment_chip == ""

    PaymentPlanFactory(lead=lead, deposit_status="paid", balance_status="na")
    lead = Lead.objects.get(pk=lead.pk)
    assert lead.payment_chip == "Deposit paid"

    plan = lead.payment
    plan.balance_status = "paid"
    plan.save(update_fields=["balance_status", "updated_at"])
    lead = Lead.objects.get(pk=lead.pk)
    assert lead.payment_chip == "Paid in full"

    plan.balance_status = "failed"
    plan.save(update_fields=["balance_status", "updated_at"])
    lead = Lead.objects.get(pk=lead.pk)
    assert lead.payment_chip == "Balance failed"
