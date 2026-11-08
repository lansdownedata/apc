"""APC-106 — the Customer Billing screen, its create/void endpoints and the nav count."""

import json
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import invoices
from apps.billing.factories import AccountGroupFactory
from apps.billing.models import Invoice
from apps.billing.tests._helpers import _processed_trip

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


@pytest.fixture
def accountant(client):
    user = UserFactory(can_manage_payments=True)
    client.force_login(user)
    client.user = user
    return client


def _two_ready(accountant):
    group = AccountGroupFactory(name="Events Team", account__name="Capitol Weddings")
    first = _processed_trip(group, accountant=accountant.user)
    second = _processed_trip(group, accountant=accountant.user)
    return group, first, second


def test_page_renders_accounts_groups_and_pane(accountant) -> None:
    group, first, second = _two_ready(accountant)
    resp = accountant.get(reverse("billing"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Customer Billing" in html
    assert "Capitol Weddings" in html
    assert "Events Team" in html
    assert f"#{first.reference}" in html
    assert f"#{second.reference}" in html
    assert "Create invoice from 2 trips" in html
    assert reverse("billing_invoice_create", args=[group.pk]) in html
    for trip in (first, second):
        for line in invoices.line_description(trip).split("\n"):
            assert line in html


def test_page_defaults_to_first_group_with_ready_trips(accountant) -> None:
    AccountGroupFactory(name="Empty Group", account__name="Aardvark Accounts")
    group = AccountGroupFactory(name="Busy Group", account__name="Busy Accounts")
    _processed_trip(group, accountant=accountant.user)
    resp = accountant.get(reverse("billing"))
    assert resp.context["group"] == group
    assert len(resp.context["trips"]) == 1
    assert resp.context["ready_count"] == 1


def test_page_without_payments_access_hides_create(staff) -> None:
    group = AccountGroupFactory()
    _processed_trip(group, accountant=UserFactory(can_manage_payments=True))
    resp = staff.get(reverse("billing"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert reverse("billing_invoice_create", args=[group.pk]) not in html
    assert "Creating invoices needs payments access." in html


def test_empty_state(staff) -> None:
    resp = staff.get(reverse("billing"))
    assert resp.status_code == 200
    assert resp.context["group"] is None
    assert "Nothing is ready to invoice." in resp.content.decode()


def test_create_endpoint_creates_and_redirects(accountant) -> None:
    group, first, second = _two_ready(accountant)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {
            "trip_ids": [first.pk, second.pk],
            "invoice_date": timezone.localdate().isoformat(),
            "po_number": "4471",
            "memo": "",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "group=" in body["redirect"] and "invoice=" in body["redirect"]
    assert Invoice.objects.count() == 1
    invoice = Invoice.objects.get()
    assert invoice.po_number == "4471"
    assert invoice.lines.count() == 2


def test_create_endpoint_refuses_foreign_trip_ids(accountant) -> None:
    group, first, _ = _two_ready(accountant)
    foreign = _processed_trip(accountant=accountant.user)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {"trip_ids": [first.pk, foreign.pk], "invoice_date": timezone.localdate().isoformat()},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert Invoice.objects.count() == 0


def test_create_endpoint_bad_date_400(accountant) -> None:
    group, first, _ = _two_ready(accountant)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {"trip_ids": [first.pk], "invoice_date": "next tuesday"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "Enter a valid invoice date."}
    assert Invoice.objects.count() == 0


def test_create_endpoint_gated(client) -> None:
    group = AccountGroupFactory()
    url = reverse("billing_invoice_create", args=[group.pk])
    data = {"trip_ids": [1], "invoice_date": timezone.localdate().isoformat()}
    anonymous = client.post(url, data)
    assert anonymous.status_code == 302
    assert reverse("login") in anonymous["Location"]
    client.force_login(UserFactory())
    assert client.post(url, data).status_code == 403
    assert Invoice.objects.count() == 0


def test_void_endpoint(accountant) -> None:
    group, first, second = _two_ready(accountant)
    invoice = invoices.create_invoice(
        group,
        [first.pk, second.pk],
        invoice_date=timezone.localdate(),
        po_number="",
        memo="",
        user=accountant.user,
    )
    url = reverse("billing_invoice_void", args=[invoice.pk])
    missing = accountant.post(url, {"reason": "  "})
    assert missing.status_code == 400
    assert missing.json()["ok"] is False
    resp = accountant.post(url, {"reason": "Wrong PO"})
    assert resp.json() == {"ok": True}
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.VOID
    assert invoices.ready_count() == 2


def test_void_endpoint_gated(client) -> None:
    accountant = UserFactory(can_manage_payments=True)
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = invoices.create_invoice(
        group, [trip.pk], invoice_date=timezone.localdate(), po_number="", memo="", user=accountant
    )
    url = reverse("billing_invoice_void", args=[invoice.pk])
    anonymous = client.post(url, {"reason": "x"})
    assert anonymous.status_code == 302
    assert reverse("login") in anonymous["Location"]
    client.force_login(UserFactory())
    assert client.post(url, {"reason": "x"}).status_code == 403
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN


def test_nav_shows_billing_count(staff) -> None:
    html = staff.get(reverse("dashboard")).content.decode()
    assert reverse("billing") in html
    assert "data-billing-badge" not in html
    _processed_trip(accountant=UserFactory(can_manage_payments=True))
    html = staff.get(reverse("dashboard")).content.decode()
    assert reverse("billing") in html
    assert 'data-billing-badge="1"' in html


def test_page_query_count_is_flat(accountant) -> None:
    _processed_trip(accountant=accountant.user)
    with CaptureQueriesContext(connection) as one:
        assert accountant.get(reverse("billing")).status_code == 200
    for _ in range(4):
        group = AccountGroupFactory()
        for _ in range(5):
            _processed_trip(group, accountant=accountant.user)
    with CaptureQueriesContext(connection) as many:
        resp = accountant.get(reverse("billing"))
    assert resp.status_code == 200
    assert sum(len(g.trips) for a in resp.context["accounts"] for g in a.groups) == 21
    assert len(many) == len(one)


def test_draft_data_is_safe_json(accountant) -> None:
    group, first, second = _two_ready(accountant)
    resp = accountant.get(reverse("billing"))
    html = resp.content.decode()
    start = html.index('id="billing-draft"')
    payload = html[html.index(">", start) + 1 : html.index("</script>", start)]
    data = json.loads(payload)
    assert [t["id"] for t in data["trips"]] == [first.pk, second.pk]
    assert data["termDays"] == 30


def test_group_row_terms_label() -> None:
    own = AccountGroupFactory(terms="net_15")
    inherited = AccountGroupFactory(terms="", account__terms="due_on_receipt")
    assert invoices.GroupRow(group=own).terms_label == "Net 15"
    assert invoices.GroupRow(group=inherited).terms_label == "Due on receipt"


def test_page_highlights_the_invoice_just_made(accountant) -> None:
    group, first, second = _two_ready(accountant)
    invoice = invoices.create_invoice(
        group,
        [first.pk],
        invoice_date=timezone.localdate(),
        po_number="",
        memo="",
        user=accountant.user,
    )
    resp = accountant.get(reverse("billing"), {"group": group.pk, "invoice": invoice.pk})
    html = resp.content.decode()
    assert resp.context["invoices"] == [invoice]
    assert "Not sent yet" in html and "QuickBooks: Not sent" in html
    assert "ring-2 ring-gold/40" in html
    assert reverse("billing_invoice_void", args=[invoice.pk]) in html
    assert "Create invoice from 1 trip<" in html
