"""APC-106 — the Customer Billing screen, its create/void endpoints and the nav count."""

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing import invoices
from apps.billing.factories import (
    AccountGroupFactory,
    InvoiceFactory,
    InvoiceLineFactory,
    QboItemFactory,
)
from apps.billing.models import Invoice, QboAccountMapping, SyncState
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


def _group_with_open_invoice(accountant, *, ready: int, invoiced: int):
    group = AccountGroupFactory()
    on_invoice = [_processed_trip(group, accountant=accountant.user) for _ in range(invoiced)]
    invoices.create_invoice(
        group,
        [t.pk for t in on_invoice],
        invoice_date=timezone.localdate(),
        po_number="",
        memo="",
        user=accountant.user,
    )
    for _ in range(ready):
        _processed_trip(group, accountant=accountant.user)
    return group


def test_page_query_count_is_flat(accountant) -> None:
    small = _group_with_open_invoice(accountant, ready=1, invoiced=1)
    with CaptureQueriesContext(connection) as one:
        assert accountant.get(reverse("billing"), {"group": small.pk}).status_code == 200
    big = _group_with_open_invoice(accountant, ready=5, invoiced=2)
    for _ in range(3):
        group = AccountGroupFactory()
        for _ in range(5):
            _processed_trip(group, accountant=accountant.user)
    with CaptureQueriesContext(connection) as many:
        resp = accountant.get(reverse("billing"), {"group": big.pk})
    assert resp.status_code == 200
    assert sum(len(g.trips) for a in resp.context["accounts"] for g in a.groups) == 21
    assert len(resp.context["trips"]) == 5
    assert [i.lines.count() for i in resp.context["invoices"]] == [2]
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


def test_draft_carries_each_trips_po(accountant) -> None:
    from apps.leads.models import Lead

    group, first, second = _two_ready(accountant)
    Lead.objects.filter(pk=second.lead_id).update(po_number="PO-9")
    resp = accountant.get(reverse("billing"), {"group": group.pk})
    trips = resp.context["draft"]["trips"]
    assert [(t["id"], t["po"]) for t in trips] == [(first.pk, ""), (second.pk, "PO-9")]
    assert "These trips carry different PO numbers." in resp.content.decode()


def test_draft_carries_the_page_filters(accountant) -> None:
    group, first, _ = _two_ready(accountant)
    resp = accountant.get(reverse("billing"), {"group": group.pk, "q": first.reference})
    assert resp.context["draft"]["filters"] == {"q": first.reference, "from": "", "to": ""}


def test_create_redirect_keeps_the_filters(accountant) -> None:
    group, first, _ = _two_ready(accountant)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {
            "trip_ids": [first.pk],
            "invoice_date": timezone.localdate().isoformat(),
            "q": "Smith & Co",
            "from": "2026-01-02",
            "to": "not a date",
        },
    )
    invoice = Invoice.objects.get()
    assert resp.json()["redirect"] == (
        f"{reverse('billing')}?group={group.pk}&invoice={invoice.pk}&q=Smith+%26+Co&from=2026-01-02"
    )


@pytest.mark.parametrize(
    ("offset", "ok"),
    [(365, True), (366, False)],
)
def test_create_bounds_the_invoice_date_ahead(accountant, offset: int, ok: bool) -> None:
    group, first, _ = _two_ready(accountant)
    day = timezone.localdate() + timedelta(days=offset)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {"trip_ids": [first.pk], "invoice_date": day.isoformat()},
    )
    if ok:
        assert resp.json()["ok"] is True
    else:
        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Enter a valid invoice date."}
    assert Invoice.objects.count() == (1 if ok else 0)


@pytest.mark.parametrize(("day", "ok"), [("2020-01-01", True), ("2019-12-31", False)])
def test_create_bounds_the_invoice_date_behind(accountant, day: str, ok: bool) -> None:
    group, first, _ = _two_ready(accountant)
    resp = accountant.post(
        reverse("billing_invoice_create", args=[group.pk]),
        {"trip_ids": [first.pk], "invoice_date": day},
    )
    if ok:
        assert resp.json()["ok"] is True
    else:
        assert resp.status_code == 400
        assert resp.json() == {"ok": False, "error": "Enter a valid invoice date."}
    assert Invoice.objects.count() == (1 if ok else 0)


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
    assert f">{invoice.label}<" in html
    assert "Not sent yet" not in html and "In QuickBooks" not in html
    assert "ring-2 ring-gold/40" in html
    assert reverse("billing_invoice_void", args=[invoice.pk]) in html
    assert "Create invoice from 1 trip<" in html


# --- APC-107: the invoice row's QuickBooks state, number and Retry -------------------------

CONNECTED = "apps.integrations.qbo.is_connected"
QBO_REQUEST = "apps.integrations.qbo.request"


@pytest.fixture
def mapping():
    config = QboAccountMapping.load()
    config.trip_item = QboItemFactory(realm_id="123", qbo_id="9")
    config.customer_docs_from = date(2026, 1, 1)
    config.save()
    return config


@pytest.fixture
def realm():
    with (
        patch("apps.billing.qbo_invoices.current_realm", return_value="123"),
        patch("apps.billing.qbo_sync.current_realm", return_value="123"),
    ):
        yield


def _open_invoice(**fields):
    group = AccountGroupFactory(qbo_id="55", qbo_realm_id="123")
    invoice = InvoiceFactory(
        group=group,
        invoice_date=date(2026, 3, 1),
        due_date=date(2026, 3, 31),
        total="920.00",
        **fields,
    )
    InvoiceLineFactory(invoice=invoice, description="Trip", amount="920.00")
    return invoice


def _qbo_answer() -> dict:
    line = {"DetailType": "SalesItemLineDetail", "Amount": 920.0}
    return {
        "Invoice": {
            "Id": "900",
            "SyncToken": "0",
            "DocNumber": "1042",
            "TxnDate": "2026-03-01",
            "TotalAmt": 920.0,
            "Line": [line, {"DetailType": "SubTotalLineDetail", "Amount": 920.0}],
        }
    }


def test_row_shows_label_chip_and_retry_on_error(accountant) -> None:
    invoice = _open_invoice(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Customer is inactive.")
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Customer is inactive." in html
    assert ">Retry<" in html
    assert reverse("billing_invoice_sync", args=[invoice.pk]) in html
    assert "Not sent yet" not in html

    invoice.qbo_sync_state, invoice.qbo_sync_error, invoice.number = SyncState.SYNCED, "", "1042"
    invoice.save()
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "In QuickBooks" in html and "Invoice 1042" in html
    assert ">Retry<" not in html

    invoice.qbo_sync_state, invoice.number = SyncState.NOT_SYNCED, ""
    invoice.save()
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Not sent yet" in html
    assert "Changed since it was sent" not in html

    # Sent, then changed here: it says so, and a person's Retry sends the change.
    invoice.qbo_id = "900"
    invoice.save()
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Changed since it was sent" in html and "Not sent yet" not in html
    assert ">Retry<" in html


def test_retry_is_hidden_without_payments_access(staff) -> None:
    invoice = _open_invoice(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Customer is inactive.")
    with patch(CONNECTED, return_value=True):
        html = staff.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Customer is inactive." in html
    assert ">Retry<" not in html


def test_row_without_connection_shows_no_quickbooks_text(accountant) -> None:
    invoice = _open_invoice(qbo_sync_state=SyncState.ERROR, qbo_sync_error="Customer is inactive.")
    with patch(CONNECTED, return_value=False):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert f'id="invoice-{invoice.pk}"' in html
    assert "In QuickBooks" not in html and "Not synced" not in html
    assert "Not sent yet" not in html
    assert "Customer is inactive." not in html
    assert ">Retry<" not in html


def test_sync_endpoint_pushes_now(accountant, mapping, realm) -> None:
    invoice = _open_invoice()
    with patch(CONNECTED, return_value=True), patch(QBO_REQUEST, return_value=_qbo_answer()):
        resp = accountant.post(reverse("billing_invoice_sync", args=[invoice.pk]))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "state": "synced", "error": "", "number": "1042"}
    invoice.refresh_from_db()
    assert invoice.number == "1042" and invoice.qbo_sync_state == SyncState.SYNCED


def test_sync_endpoint_400_when_not_connected(accountant) -> None:
    invoice = _open_invoice()
    with patch(CONNECTED, return_value=False):
        resp = accountant.post(reverse("billing_invoice_sync", args=[invoice.pk]))
    assert resp.status_code == 400


def test_sync_endpoint_gated(staff, client) -> None:
    invoice = _open_invoice()
    url = reverse("billing_invoice_sync", args=[invoice.pk])
    assert staff.post(url).status_code == 403
    client.logout()
    resp = client.post(url)
    assert resp.status_code == 302 and "login" in resp["Location"]


def test_sync_endpoint_502_on_outage(accountant, mapping, realm) -> None:
    from apps.integrations import qbo

    invoice = _open_invoice()
    with (
        patch(CONNECTED, return_value=True),
        patch(QBO_REQUEST, side_effect=qbo.QboAPIError(503, "down")),
    ):
        resp = accountant.post(reverse("billing_invoice_sync", args=[invoice.pk]))
    assert resp.status_code == 502
    assert resp.json()["error"] == "QuickBooks couldn't be reached. Try again in a minute."


def test_void_confirm_says_quickbooks_voids_it_too(accountant) -> None:
    sent = _open_invoice(qbo_id="900", qbo_sync_state=SyncState.SYNCED, number="1042")
    unsent = InvoiceFactory(group=sent.group, total="0")
    InvoiceLineFactory(invoice=unsent, description="Trip", amount="0")
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": sent.group_id}).content.decode()
    void_sent = reverse("billing_invoice_void", args=[sent.pk])
    void_unsent = reverse("billing_invoice_void", args=[unsent.pk])
    assert f"voidInvoice('{void_sent}', &quot;Invoice 1042&quot;, true)" in html
    assert f"voidInvoice('{void_unsent}', &quot;{unsent.label}&quot;, false)" in html
    assert "is voided in QuickBooks too, on the next sync." in html


def test_row_before_the_start_date_says_so(accountant, mapping) -> None:
    invoice = _open_invoice()
    Invoice.objects.filter(pk=invoice.pk).update(invoice_date=date(2025, 12, 31))
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Before the QuickBooks start date" in html
    assert "Not sent yet" not in html


def _refused_void():
    invoice = _open_invoice(qbo_id="900", qbo_realm_id="123", number="1042")
    invoices.void_invoice(invoice, reason="Wrong PO", user=None)
    Invoice.objects.filter(pk=invoice.pk).update(
        qbo_sync_state=SyncState.ERROR,
        qbo_sync_error="QuickBooks didn't accept this void: It has a payment",
    )
    invoice.refresh_from_db()
    return invoice


def test_group_pane_lists_a_refused_void_with_retry(accountant) -> None:
    invoice = _refused_void()
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Voids waiting for QuickBooks" in html
    assert "Invoice 1042" in html
    assert f"Voided {timezone.localtime(invoice.voided_at):%b %-d, %Y}" in html
    assert "QuickBooks didn&#x27;t accept this void: It has a payment" in html
    assert f"retryQboSync('{reverse('billing_invoice_sync', args=[invoice.pk])}')" in html


def test_group_pane_void_section_hidden_without_voids_or_connection(accountant) -> None:
    group = _open_invoice().group
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": group.pk}).content.decode()
    assert "Voids waiting for QuickBooks" not in html

    invoice = _refused_void()
    with patch(CONNECTED, return_value=False):
        html = accountant.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Voids waiting for QuickBooks" not in html


def test_retry_on_a_waiting_void_needs_payments_access(staff) -> None:
    invoice = _refused_void()
    with patch(CONNECTED, return_value=True):
        html = staff.get(reverse("billing"), {"group": invoice.group_id}).content.decode()
    assert "Voids waiting for QuickBooks" in html
    assert "retryQboSync(" not in html


# --- APC-109: Edit and Void and replace on the row -----------------------------------------


def _made(accountant, group=None, **fields):
    """An open invoice over one real processed trip, made the way the page makes one."""
    group = group or AccountGroupFactory(name="Events Team")
    trip = _processed_trip(group, accountant=accountant.user)
    invoice = invoices.create_invoice(
        group,
        [trip.pk],
        invoice_date=date(2026, 11, 1),
        po_number="4471",
        memo="Nov",
        user=accountant.user,
    )
    if fields:
        Invoice.objects.filter(pk=invoice.pk).update(**fields)
        invoice.refresh_from_db()
    return invoice


def _close_books(through: date) -> None:
    QboAccountMapping.objects.update_or_create(pk=1, defaults={"book_close_date": through})


def test_edit_endpoint_updates_and_redirects(accountant) -> None:
    invoice = _made(accountant)
    url = reverse("billing_invoice_edit", args=[invoice.pk])
    resp = accountant.post(
        url,
        {"invoice_date": "2026-11-05", "po_number": " 4480 ", "memo": "Fixed", "q": "Ana"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert f"group={invoice.group_id}" in body["redirect"]
    assert f"invoice={invoice.pk}" in body["redirect"] and "q=Ana" in body["redirect"]
    invoice.refresh_from_db()
    assert (invoice.invoice_date, invoice.po_number, invoice.memo) == (
        date(2026, 11, 5),
        "4480",
        "Fixed",
    )
    assert invoice.edited_by == accountant.user
    refused = accountant.post(
        url, {"invoice_date": "2026-11-05", "po_number": "9" * 65, "memo": "Fixed"}
    )
    assert refused.status_code == 400
    assert refused.json() == {
        "ok": False,
        "error": "The PO number is too long (64 characters at most).",
    }


def test_replace_endpoint_creates_new_and_highlights_it(accountant) -> None:
    old = _made(accountant, number="1042")
    url = reverse("billing_invoice_replace", args=[old.pk])
    missing = accountant.post(url, {"reason": "  ", "invoice_date": "2026-11-05"})
    assert missing.status_code == 400
    assert missing.json()["error"] == "Say why the invoice is voided."
    old.refresh_from_db()
    assert old.status == Invoice.Status.OPEN

    resp = accountant.post(url, {"reason": "Wrong PO", "po_number": "", "memo": ""})
    assert resp.status_code == 200
    old.refresh_from_db()
    new = old.replaced_by
    assert old.status == Invoice.Status.VOID and new is not None
    assert resp.json() == {
        "ok": True,
        "redirect": f"{reverse('billing')}?group={old.group_id}&invoice={new.pk}",
    }
    # The date defaults to today, the PO carries over, the memo names the old invoice.
    assert new.invoice_date == timezone.localdate()
    assert (new.po_number, new.memo) == ("4471", "Replaces Invoice 1042")


def test_endpoints_gated(client) -> None:
    accountant = UserFactory(can_manage_payments=True)
    client.force_login(accountant)
    client.user = accountant
    invoice = _made(client)
    client.logout()
    for name in ("billing_invoice_edit", "billing_invoice_replace"):
        url = reverse(name, args=[invoice.pk])
        data = {"reason": "x", "invoice_date": "2026-11-05", "po_number": "1", "memo": "x"}
        anonymous = client.post(url, data)
        assert anonymous.status_code == 302
        assert reverse("login") in anonymous["Location"]
        client.force_login(UserFactory())
        assert client.post(url, data).status_code == 403
        client.logout()
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN and invoice.edited_at is None


def test_bad_date_400(accountant) -> None:
    invoice = _made(accountant)
    for name in ("billing_invoice_edit", "billing_invoice_replace"):
        url = reverse(name, args=[invoice.pk])
        for bad in ("next tuesday", "2019-12-31"):
            resp = accountant.post(url, {"reason": "Wrong PO", "invoice_date": bad})
            assert resp.status_code == 400
            assert resp.json() == {"ok": False, "error": "Enter a valid invoice date."}
    # Edit has no default date: a missing one is refused too.
    resp = accountant.post(reverse("billing_invoice_edit", args=[invoice.pk]), {"memo": "x"})
    assert resp.json() == {"ok": False, "error": "Enter a valid invoice date."}
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OPEN and invoice.edited_at is None


def test_closed_period_rows_hide_actions_and_show_tag(accountant) -> None:
    closed = _made(accountant)
    current = _made(accountant, group=closed.group)
    Invoice.objects.filter(pk=current.pk).update(invoice_date=date(2026, 11, 10))
    _close_books(date(2026, 11, 3))
    resp = accountant.get(reverse("billing"), {"group": closed.group_id})
    html = resp.content.decode()
    assert resp.context["book_close_date"] == date(2026, 11, 3)
    assert html.count("In a closed period") == 1
    for name in ("billing_invoice_edit", "billing_invoice_replace", "billing_invoice_void"):
        assert reverse(name, args=[closed.pk]) not in html
        assert reverse(name, args=[current.pk]) in html
    assert ">Edit<" in html and ">Void and replace<" in html


def test_row_actions_need_payments_access(staff) -> None:
    accountant = UserFactory(can_manage_payments=True)
    group = AccountGroupFactory()
    trip = _processed_trip(group, accountant=accountant)
    invoice = invoices.create_invoice(
        group, [trip.pk], invoice_date=date(2026, 11, 1), po_number="", memo="", user=accountant
    )
    html = staff.get(reverse("billing"), {"group": group.pk}).content.decode()
    assert f'id="invoice-{invoice.pk}"' in html
    assert ">Edit<" not in html and ">Void and replace<" not in html


def test_row_shows_edited_and_replaced_links(accountant) -> None:
    accountant.user.first_name = "Ana"
    accountant.user.save()
    old = _made(accountant, number="1042", qbo_id="900", qbo_realm_id="123")
    with patch("apps.billing.qbo_sync.current_realm", return_value="123"):
        new = invoices.void_and_replace(
            old,
            reason="Wrong PO",
            invoice_date=date(2026, 11, 5),
            po_number="",
            memo="",
            user=accountant.user,
        )
    invoices.edit_invoice(
        new, invoice_date=date(2026, 11, 6), po_number="4471", memo="Fixed", user=accountant.user
    )
    new.refresh_from_db()
    with patch(CONNECTED, return_value=True):
        html = accountant.get(reverse("billing"), {"group": old.group_id}).content.decode()
    edited = timezone.localtime(new.edited_at)
    assert f"Edited {edited:%b} {edited.day} by Ana" in html
    base = f"{reverse('billing')}?group={old.group_id}&amp;invoice="
    assert f'href="{base}{old.pk}"' in html and "Replaces Invoice 1042" in html
    # The old one waits in "Voids waiting for QuickBooks" and points at its replacement.
    assert "Voids waiting for QuickBooks" in html
    assert f'href="{base}{new.pk}"' in html and f"Replaced by {new.label}" in html


def test_edit_modal_fields_prefilled(accountant) -> None:
    group = AccountGroupFactory(po_required=True)
    invoice = _made(accountant, group=group, memo='Say "hi" <b>')
    html = accountant.get(reverse("billing"), {"group": group.pk}).content.decode()
    assert 'data-date="2026-11-01"' in html
    assert 'data-po="4471"' in html
    assert 'data-memo="Say &quot;hi&quot; &lt;b&gt;"' in html
    assert 'data-po-required="true"' in html
    assert f'data-label="{invoice.label}"' in html
    assert f'data-edit-url="{reverse("billing_invoice_edit", args=[invoice.pk])}"' in html
    assert f'data-replace-url="{reverse("billing_invoice_replace", args=[invoice.pk])}"' in html
    assert 'Say "hi" <b>' not in html
