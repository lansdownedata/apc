"""APC-107: customer documents (invoices, later card receipts) pushed to QuickBooks.

Outbound only. The company's Product/Service list is cached here so staff choose the one every
trip line posts to from Settings -> QuickBooks, and the choice is checked against the connected
company before it is used.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.billing.models import Invoice, InvoiceLine, QboAccountMapping, QboItem, SyncState
from apps.billing.qbo_sync import (
    STALE_OBJECT,
    SYNC_BATCH,
    SyncPaused,
    _fault_code,
    _transient,
    current_realm,
    term_ref,
)
from apps.integrations import qbo
from apps.payments.qbo_bills import (
    _fail,
    _leased,
    _Link,
    _push,
    _reason,
    _Refused,
    _same_total,
    _stale,
)

logger = logging.getLogger(__name__)

# --- the Product/Service cache -------------------------------------------------------------------

# Intuit's query pages: 1-based STARTPOSITION, at most 1000 rows a page.
ITEM_PAGE = 1000


def _item_rows() -> list[dict]:
    """Every service and non-inventory item in the company, page by page until a short page."""
    rows: list[dict] = []
    position = 1
    while True:
        query = (
            "select * from Item where Type in ('Service', 'NonInventory')"
            f" startposition {position} maxresults {ITEM_PAGE}"
        )
        page = qbo.request("GET", "query", params={"query": query})
        found = page.get("QueryResponse", {}).get("Item", [])
        rows.extend(found)
        if len(found) < ITEM_PAGE:
            return rows
        position += ITEM_PAGE


def refresh_items() -> int:
    """Cache the company's products and services; one no longer there is retired, not deleted
    (a mapping that pointed at it then reads as unset). Every page is read before anything is
    retired, so a failure part-way retires nothing. Returns how many are active."""
    realm = current_realm()
    seen = []
    for row in _item_rows():
        QboItem.objects.update_or_create(
            realm_id=realm,
            qbo_id=str(row["Id"]),
            defaults={
                "name": (row.get("FullyQualifiedName") or row.get("Name") or "")[:255],
                "item_type": row.get("Type", ""),
                "active": bool(row.get("Active", True)),
            },
        )
        seen.append(str(row["Id"]))
    QboItem.objects.filter(realm_id=realm).exclude(qbo_id__in=seen).update(active=False)
    return QboItem.objects.filter(realm_id=realm, active=True).count()


def _usable():
    return QboItem.objects.filter(realm_id=current_realm(), active=True)


def item_choices() -> list[tuple[int, str]]:
    return list(_usable().values_list("pk", "name"))


def trip_item_ref() -> dict | None:
    """The Product/Service trip lines post to, or None until one is chosen for this company."""
    item = QboAccountMapping.load().trip_item
    if item is None or not _usable().filter(pk=item.pk).exists():
        return None
    return {"value": item.qbo_id}


def customer_docs_from() -> date | None:
    """Documents dated before this are never sent; None until staff choose a start date."""
    return QboAccountMapping.load().customer_docs_from


# --- the Invoice payload -------------------------------------------------------------------------

# QuickBooks' limit for a line Description and for PrivateNote.
DESCRIPTION_MAX = 4000
# QuickBooks' limit for DocNumber (we send none; QuickBooks numbers the invoice itself).
DOC_NUMBER_MAX = 21

NO_TRIP_ITEM = "Waiting: choose the Product/Service for trip charges in Settings \u2192 QuickBooks."
NO_START_DATE = (
    "Waiting: choose the start date for customer documents in Settings \u2192 QuickBooks."
)
NO_LINES = "This invoice has no lines, so it isn't sent."


def _money(amount) -> str:
    return f"{amount:.2f}"


def invoice_payload(invoice: Invoice, realm: str) -> dict:
    """The QuickBooks Invoice body for one of our invoices. Optional keys are left out when
    blank. The PO rides on the note and on the first line's description."""
    item = trip_item_ref()
    if item is None:
        raise _Refused(NO_TRIP_ITEM, alert=False)
    # Read once: a void committing after the pending check empties the lines, and QuickBooks
    # must never get an invoice with none.
    rows = list(invoice.lines.order_by("pk"))
    if not rows:
        raise _Refused(NO_LINES, alert=False)
    group = invoice.group
    po = f"PO {invoice.po_number}" if invoice.po_number else ""
    lines = []
    for index, line in enumerate(rows):
        description = f"{po} \u00b7 {line.description}" if po and index == 0 else line.description
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": _money(line.amount),
                "Description": description[:DESCRIPTION_MAX],
                "SalesItemLineDetail": {
                    "ItemRef": item,
                    "Qty": 1,
                    "UnitPrice": _money(line.amount),
                },
            }
        )
    body: dict = {
        "CustomerRef": {"value": group.qbo_id},
        "TxnDate": invoice.invoice_date.isoformat(),
        "DueDate": invoice.due_date.isoformat(),
        "Line": lines,
    }
    term = term_ref(group.effective_terms)
    if term is not None:
        body["SalesTermRef"] = term
    if group.invoice_email:
        body["BillEmail"] = {"Address": group.invoice_email}
    note = " \u00b7 ".join(part for part in (po, invoice.memo) if part)
    if note:
        body["PrivateNote"] = note[:DESCRIPTION_MAX]
    return body


def invoice_matches(obj: dict, body: dict) -> bool:
    """QuickBooks' Invoice is the one we sent: same total to the cent and the same number of
    item lines (QuickBooks adds its own subtotal line, which is not ours)."""
    expected = sum((Decimal(line["Amount"]) for line in body["Line"]), Decimal("0"))
    if not _same_total(obj, expected):
        return False
    items = [
        line for line in obj.get("Line", []) if line.get("DetailType") == "SalesItemLineDetail"
    ]
    return len(items) == len(body["Line"])


# --- the push ------------------------------------------------------------------------------------


def pending_invoices(realm: str, start: date | None):
    """Open invoices not in step with this company, once their group is in it. Those dated
    before `start` are never sent (no error); with no start date (None) every one comes back,
    for the run to mark as waiting on it. An invoice with no lines is never sent."""
    rows = (
        Invoice.objects.filter(status=Invoice.Status.OPEN)
        .filter(_stale("qbo_", realm))
        .exclude(group__qbo_id="")
        .filter(group__qbo_realm_id=realm)
        .filter(Exists(InvoiceLine.objects.filter(invoice=OuterRef("pk"))))
    )
    if start is not None:
        rows = rows.filter(invoice_date__gte=start)
    return rows.select_related("group").order_by("pk")


def pending_voids(realm: str):
    """Invoices voided here whose QuickBooks invoice (in this company) isn't void yet."""
    return (
        Invoice.objects.filter(status=Invoice.Status.VOID, qbo_realm_id=realm)
        .exclude(qbo_id="")
        .exclude(qbo_void_requested_at=None)
        .exclude(qbo_sync_state=SyncState.SYNCED)
        .select_related("group")
        .order_by("pk")
    )


def _in_step(invoice: Invoice, realm: str) -> bool:
    return (
        bool(invoice.qbo_id)
        and invoice.qbo_realm_id == realm
        and invoice.qbo_sync_state == SyncState.SYNCED
    )


def _push_invoice(invoice: Invoice, realm: str) -> bool:
    """Create (or update) the QuickBooks Invoice; QuickBooks numbers it and the number comes
    back onto ours."""
    returned: dict = {}

    def matches(obj: dict, body: dict) -> bool:
        returned.update(obj)
        return invoice_matches(obj, body)

    in_step = _push(
        _Link(invoice, "invoice", "Invoice"),
        lambda: invoice_payload(invoice, realm),
        matches=matches,
        what="invoice",
    )
    # Whatever came back is the invoice QuickBooks holds, so its number is ours to show.
    number = str(returned.get("DocNumber") or "")[:DOC_NUMBER_MAX]
    if number and number != invoice.number:
        Invoice.objects.filter(pk=invoice.pk).update(number=number)
        invoice.number = number
    return in_step


def push_invoice(invoice: Invoice) -> bool:
    """Send one invoice to QuickBooks, holding its lease. True when it is now in step; False
    with no call while another push has it, or while it isn't one to send (void, before the
    start date, its group not in QuickBooks yet). Raises SyncPaused / QboNotConnected."""
    with _leased(Invoice, invoice.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        invoice.refresh_from_db()
        realm = current_realm()
        start = customer_docs_from()
        if not pending_invoices(realm, start).filter(pk=invoice.pk).exists():
            return _in_step(invoice, realm)
        if start is None:
            _fail(_Link(invoice, "invoice", "Invoice"), NO_START_DATE, alert=False)
            return False
        return _push_invoice(invoice, realm)


def _void(invoice: Invoice) -> dict:
    """POST the void; on a stale SyncToken (the invoice was changed in QuickBooks, e.g. sent
    from there) take the current one and try once more.

    The requestid is keyed by the SyncToken, as APC-74 keys updates: a lost success replays
    under the same token, a re-read token is a new request (never the stale attempt's cached
    fault), and a void that landed bumps the token, so it can't be sent twice."""

    def send() -> dict:
        requestid = f"apc-i{invoice.pk}-void-u{invoice.qbo_sync_token}"[:50]
        return qbo.request(
            "POST",
            "invoice",
            params={"operation": "void", "requestid": requestid},
            json={"Id": invoice.qbo_id, "SyncToken": invoice.qbo_sync_token},
        )["Invoice"]

    try:
        return send()
    except qbo.QboAPIError as exc:
        if _fault_code(exc) != STALE_OBJECT:
            raise
    current = qbo.request("GET", f"invoice/{invoice.qbo_id}")["Invoice"]
    invoice.qbo_sync_token = str(current["SyncToken"])
    return send()


def _push_void(invoice: Invoice) -> bool:
    link = _Link(invoice, "invoice", "Invoice")
    try:
        obj = _void(invoice)
    except qbo.QboAPIError as exc:
        logger.warning("QuickBooks refused void of invoice %s: %s", invoice.pk, exc.body[:500])
        if _transient(exc):
            raise SyncPaused(str(exc)) from exc
        _fail(link, _reason(exc, "void"))
        return False
    link.update(
        sync_state=SyncState.SYNCED,
        sync_error="",
        sync_token=str(obj["SyncToken"]),
        synced_at=timezone.now(),
    )
    invoice.refresh_from_db()
    return True


def push_void(invoice: Invoice) -> bool:
    """Void one invoice's QuickBooks invoice, holding its lease. True when it is void there;
    False with no call while another push has it or it isn't a queued void in this company.
    Raises SyncPaused / QboNotConnected."""
    with _leased(Invoice, invoice.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        invoice.refresh_from_db()
        realm = current_realm()
        if not pending_voids(realm).filter(pk=invoice.pk).exists():
            return invoice.status == Invoice.Status.VOID and _in_step(invoice, realm)
        return _push_void(invoice)


def _block_all(rows: list[Invoice], message: str) -> bool:
    """Every pending invoice shares one missing setting: mark each (so the row says why). True
    when any is newly marked, so the run alerts once rather than every hour."""
    fresh = [
        row
        for row in rows
        if row.qbo_sync_state != SyncState.ERROR or row.qbo_sync_error != message
    ]
    if fresh:
        Invoice.objects.filter(pk__in=[row.pk for row in rows]).update(
            qbo_sync_state=SyncState.ERROR, qbo_sync_error=message
        )
    return bool(fresh)


def _alert_blocked(message: str) -> None:
    from apps.notifications.models import Notification

    Notification.notify(
        None,
        Notification.Kind.SYNC_FAILED,
        title="Customer invoices can't go to QuickBooks yet",
        detail=message,
    )


def sync_pending() -> int:
    """Push up to SYNC_BATCH invoices and voids that aren't in step: creates in pk order, then
    voids (a void never needs the start date or the item). Returns how many landed. Stops
    quietly on an outage, a rate limit or a lost connection; nothing is marked."""
    if not qbo.is_connected():
        return 0
    realm = current_realm()
    start = customer_docs_from()
    pushed = attempted = 0
    blocked = ""  # newly blocked by a missing setting: alert once for the run
    try:
        rows = list(pending_invoices(realm, start))
        if rows and start is None:
            blocked = NO_START_DATE if _block_all(rows, NO_START_DATE) else ""
        elif rows and trip_item_ref() is None:
            blocked = NO_TRIP_ITEM if _block_all(rows, NO_TRIP_ITEM) else ""
        else:
            for invoice in rows:
                if attempted >= SYNC_BATCH:
                    return pushed
                attempted += 1
                pushed += push_invoice(invoice)
        for invoice in pending_voids(realm):
            if attempted >= SYNC_BATCH:
                return pushed
            attempted += 1
            pushed += push_void(invoice)
    except (SyncPaused, qbo.QboNotConnected) as exc:
        logger.warning("QuickBooks invoice sync paused: %s", exc)
    finally:
        if blocked:
            _alert_blocked(blocked)
    return pushed


def sync_state(invoice: Invoice, *, connected: bool | None = None) -> dict | None:
    """What the invoice row shows: its state, its sentence and where Retry posts. None while
    QuickBooks isn't connected. Pass `connected` when building many."""
    from django.urls import reverse

    if connected is None:
        connected = qbo.is_connected()
    if not connected:
        return None
    return {
        "state": invoice.qbo_sync_state,
        "error": invoice.qbo_sync_error,
        "sync_url": reverse("billing_invoice_sync", args=[invoice.pk]),
    }
