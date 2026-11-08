"""APC-107: customer documents (invoices, later card receipts) pushed to QuickBooks.

Outbound only. The company's Product/Service list is cached here so staff choose the one every
trip line posts to from Settings -> QuickBooks, and the choice is checked against the connected
company before it is used.
"""

from __future__ import annotations

from datetime import date

from apps.billing.models import QboAccountMapping, QboItem
from apps.billing.qbo_sync import current_realm
from apps.integrations import qbo

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
