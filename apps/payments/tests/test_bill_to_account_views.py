"""APC-105: choosing how an order is paid, from its money card (staff only, payments access)."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.billing import services as billing
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def cashier(client):
    client.force_login(UserFactory(can_manage_payments=True))
    return client


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _order(*, account=True, po_required=False):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    TransferReservationFactory(lead=lead)
    PaymentPlanFactory(lead=lead)
    group = None
    if account:
        group = billing.create_account(
            lead.contact,
            name="Ridgeline Partners",
            group_name="Accounts Payable",
            invoice_email="ap@ridgeline.example",
            po_required=po_required,
        ).groups.get()
    return lead, group


def _to_account(client, lead, group, po=""):
    return client.post(
        reverse("order_bill_to_account", args=[lead.pk]), {"group": group.pk, "po_number": po}
    )


def test_a_payments_user_bills_an_order_to_a_group(cashier):
    lead, group = _order()

    resp = _to_account(cashier, lead, group, po="4471")

    assert resp.json() == {"ok": True}
    lead.refresh_from_db()
    assert (lead.billing_group, lead.po_number) == (group, "4471")


def test_a_refusal_comes_back_as_an_error(cashier):
    lead, group = _order(po_required=True)

    resp = _to_account(cashier, lead, group)

    assert resp.status_code == 400
    assert resp.json()["error"] == "Accounts Payable needs a PO number on every order."


def test_an_unknown_group_is_an_error_not_a_500(cashier):
    lead, _ = _order()
    resp = cashier.post(reverse("order_bill_to_account", args=[lead.pk]), {"group": "x"})
    assert resp.status_code == 400


def test_back_to_card(cashier):
    lead, group = _order()
    _to_account(cashier, lead, group)

    resp = cashier.post(reverse("order_bill_to_card", args=[lead.pk]))

    assert resp.json() == {"ok": True}
    assert not Lead.objects.get(pk=lead.pk).on_account


@pytest.mark.parametrize("name", ["order_bill_to_account", "order_bill_to_card"])
def test_both_need_payments_access(staff, name):
    lead, group = _order()
    resp = staff.post(reverse(name, args=[lead.pk]), {"group": group.pk})
    assert resp.status_code == 403
    assert not Lead.objects.get(pk=lead.pk).on_account


@pytest.mark.parametrize("page", ["order_detail", "lead_detail"])
def test_a_card_order_offers_bill_to_account(cashier, page):
    lead, _ = _order()
    html = cashier.get(reverse(page, args=[lead.pk])).content.decode()

    assert "Paid by" in html
    assert "Bill to account instead" in html
    assert "Take payment" in html


@pytest.mark.parametrize("page", ["order_detail", "lead_detail"])
def test_an_account_order_shows_who_is_billed_and_no_card_actions(cashier, page):
    lead, group = _order()
    _to_account(cashier, lead, group, po="4471")

    html = cashier.get(reverse(page, args=[lead.pk])).content.decode()

    assert "Ridgeline Partners — Accounts Payable · PO 4471" in html
    assert "ap@ridgeline.example" in html
    assert "Not yet invoiced" in html
    for name in ("order_admin_intent", "order_send_pay_link", "order_retry_balance"):
        assert reverse(name, args=[lead.pk]) not in html


def test_no_account_points_to_the_profile(cashier):
    lead, _ = _order(account=False)
    html = cashier.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert "No billing account" in html
    assert reverse("contact_detail", args=[lead.contact.pk]) in html
    assert "Bill to account instead" not in html


def test_without_payments_access_the_choice_reads_but_cannot_change(staff):
    lead, group = _order()
    billing.bill_to_account(lead, group, po_number="", user=None)

    html = staff.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert "Ridgeline Partners — Accounts Payable" in html
    assert "billToAccount(" not in html


def test_refund_and_cancel_stay_on_an_account_order(cashier):
    lead, group = _order()
    billing.bill_to_account(lead, group, po_number="", user=None)

    html = cashier.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert reverse("order_refund", args=[lead.pk]) in html
    assert reverse("order_cancel_refund", args=[lead.pk]) in html


def test_the_staff_card_endpoints_refuse_an_account_order(cashier, settings):
    settings.OUTBOUND_ENABLED = True
    lead, group = _order()
    billing.bill_to_account(lead, group, po_number="", user=None)

    for name in ("order_admin_intent", "order_charge_saved", "order_send_pay_link"):
        resp = cashier.post(reverse(name, args=[lead.pk]), {"amount": "10.00"})
        assert resp.status_code == 400, name
        assert "billed to an account" in resp.json()["error"]


def test_the_order_page_query_count_does_not_grow_with_an_account(cashier):
    lead, group = _order()
    cashier.get(reverse("order_detail", args=[lead.pk]))  # warm caches (settings rows)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as card:
        cashier.get(reverse("order_detail", args=[lead.pk]))
    billing.bill_to_account(lead, group, po_number="", user=None)
    with CaptureQueriesContext(connection) as account:
        cashier.get(reverse("order_detail", args=[lead.pk]))

    assert len(account) <= len(card)


def test_the_orders_list_says_on_account(cashier):
    lead, group = _order()
    billing.bill_to_account(lead, group, po_number="", user=None)

    html = cashier.get(reverse("orders_list")).content.decode()

    assert "On account" in html
