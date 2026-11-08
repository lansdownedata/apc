"""APC-107: customer documents (invoices, later card receipts) pushed to QuickBooks.

Outbound only. The company's Product/Service list is cached here so staff choose the one every
trip line posts to from Settings -> QuickBooks, and the choice is checked against the connected
company before it is used.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from apps.billing.models import Invoice, QboAccountMapping, QboItem
from apps.billing.qbo_sync import current_realm, term_ref
from apps.integrations import qbo
from apps.payments.qbo_bills import _Refused, _same_total

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


def _money(amount) -> str:
    return f"{amount:.2f}"


def invoice_payload(invoice: Invoice, realm: str) -> dict:
    """The QuickBooks Invoice body for one of our invoices. Optional keys are left out when
    blank. The PO rides on the note and on the first line's description."""
    item = trip_item_ref()
    if item is None:
        raise _Refused(NO_TRIP_ITEM, alert=False)
    group = invoice.group
    po = f"PO {invoice.po_number}" if invoice.po_number else ""
    lines = []
    for index, line in enumerate(invoice.lines.order_by("pk")):
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
