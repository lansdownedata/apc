"""APC-107: customer documents (invoices, later card receipts) pushed to QuickBooks.

Outbound only. The company's Product/Service list is cached here so staff choose the one every
trip line posts to from Settings -> QuickBooks, and the choice is checked against the connected
company before it is used.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.billing.invoices import CLOSED_PERIOD_MESSAGE, closed_in_quickbooks
from apps.billing.models import Invoice, InvoiceLine, QboAccountMapping, QboItem, SyncState
from apps.billing.qbo_sync import (
    STALE_OBJECT,
    SYNC_BATCH,
    PushBudget,
    SyncPaused,
    _fault_code,
    _transient,
    current_realm,
    term_ref,
)
from apps.integrations import qbo
from apps.payments.qbo_bills import (
    _cents,
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


def refresh_preferences() -> date | None:
    """Cache the date the books are closed through in QuickBooks (APC-109). None, and the
    cached date cleared, when QuickBooks has none or sends one that isn't a date; an answer
    without the key path, or a date that isn't one, is logged."""
    prefs = qbo.request("GET", "preferences")
    section = prefs.get("Preferences") if isinstance(prefs, dict) else None
    info = section.get("AccountingInfoPrefs") if isinstance(section, dict) else None
    raw = info.get("BookCloseDate") if isinstance(info, dict) else None
    try:
        closed = parse_date(raw) if isinstance(raw, str) else None
    except ValueError:  # well-formed but impossible, like Feb 30
        closed = None
    if not isinstance(info, dict) or (raw is not None and closed is None):
        logger.warning(
            "QuickBooks preferences carry no readable BookCloseDate; treating the books as open"
        )
    config = QboAccountMapping.load()
    config.book_close_date = closed
    config.save(update_fields=["book_close_date"])
    return closed


def refresh_preferences_quietly() -> None:
    """`refresh_preferences`, best-effort: a failed read keeps the cached closing date. Run
    before a push so it is judged on the closing date QuickBooks has now."""
    try:
        refresh_preferences()
    except (qbo.QboAPIError, qbo.QboNotConnected):
        logger.warning("QuickBooks preferences refresh failed; keeping the cached closing date")


def customer_docs_from() -> date | None:
    """Documents dated before this are never sent; None until staff choose a start date."""
    return QboAccountMapping.load().customer_docs_from


# --- the Invoice payload -------------------------------------------------------------------------

ITEM_LINE = "SalesItemLineDetail"
# QuickBooks' limit for a line Description and for PrivateNote.
DESCRIPTION_MAX = 4000
# QuickBooks' limit for DocNumber. A create sends none (QuickBooks numbers the invoice); an
# update sends back the number QuickBooks gave.
DOC_NUMBER_MAX = 21

NO_TRIP_ITEM = "Waiting: choose the Product/Service for trip charges in Settings \u2192 QuickBooks."
NO_START_DATE = (
    "Waiting: choose the start date for customer documents in Settings \u2192 QuickBooks."
)
NO_LINES = "This invoice has no lines, so it isn't sent."
LINES_OFF_TOTAL = "This invoice's lines don't add up to its total."
# An update (an edit, or a Retry of one in QuickBooks) refused at push time: QuickBooks holds
# it, or would, in a closed period. A refusal on the page says `CLOSED_PERIOD_MESSAGE`.
CLOSED_UPDATE_MESSAGE = "This edit didn't reach QuickBooks: the period is closed there."


def _money(amount) -> str:
    return f"{amount:.2f}"


def invoice_payload(invoice: Invoice, realm: str) -> dict:
    """The QuickBooks Invoice body for one of our invoices. Optional keys are left out when
    blank. The PO rides on the note and on the first line's description. An update (already in
    this company) is sparse: it carries its number, and a blank note so a cleared PO and memo
    clear it there too (a key left out keeps what QuickBooks has)."""
    item = trip_item_ref()
    if item is None:
        raise _Refused(NO_TRIP_ITEM, alert=False)
    # Read once: a void committing after the pending check empties the lines, and QuickBooks
    # must never get an invoice with none.
    rows = list(invoice.lines.order_by("pk"))
    if not rows:
        raise _Refused(NO_LINES, alert=False)
    if sum((line.amount for line in rows), Decimal("0")) != invoice.total:
        raise _Refused(LINES_OFF_TOTAL, alert=False)
    group = invoice.group
    po = f"PO {invoice.po_number}" if invoice.po_number else ""
    lines = []
    for index, line in enumerate(rows):
        description = f"{po} \u00b7 {line.description}" if po and index == 0 else line.description
        lines.append(
            {
                "DetailType": ITEM_LINE,
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
    updating = _linked_here(invoice, realm)
    if note or updating:
        body["PrivateNote"] = note[:DESCRIPTION_MAX]
    if updating and invoice.number:
        body["DocNumber"] = invoice.number
    return body


def _mismatch(obj: dict, body: dict, invoice: Invoice) -> str:
    """Why what QuickBooks holds isn't what we sent, in a sentence staff can act on."""
    if not _same_total(obj, invoice.total):
        held = _cents(obj.get("TotalAmt"))
        if held is not None:
            return (
                f"QuickBooks totals this invoice ${held:,.2f}, not ${invoice.total:,.2f}. "
                "Check sales tax on the Product/Service."
            )
    items = sum(1 for line in obj.get("Line", []) if line.get("DetailType") == ITEM_LINE)
    return (
        f"QuickBooks holds this invoice with {items} lines, not {len(body['Line'])}. "
        "Check it in QuickBooks."
    )


def invoice_matches(obj: dict, body: dict) -> bool:
    """QuickBooks' Invoice is the one we sent: same total to the cent and the same number of
    item lines (QuickBooks adds its own subtotal line, which is not ours)."""
    expected = sum((Decimal(line["Amount"]) for line in body["Line"]), Decimal("0"))
    if not _same_total(obj, expected):
        return False
    items = [line for line in obj.get("Line", []) if line.get("DetailType") == ITEM_LINE]
    return len(items) == len(body["Line"])


# --- the push ------------------------------------------------------------------------------------


def unsent_first(rows, prefix: str = "qbo_"):
    """Not-yet-sent rows ahead of refused ones, each in pk order: a pile of refusals retried
    every hour never starves a new record out of the batch."""
    refused = Case(
        When(**{f"{prefix}sync_state": SyncState.ERROR}, then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    return rows.alias(refused_rank=refused).order_by("refused_rank", "pk")


def pending_invoices(realm: str, start: date | None, *, linked: bool = False):
    """Open invoices not in step with this company, once their group is in it, not yet sent
    ahead of refused (`unsent_first`). Those dated before `start` are never sent (no error);
    with no start date (None) every one comes back, for the run to mark as waiting on it. An
    invoice with no lines is never sent.

    One already in this company is left out unless `linked` or an edit of it is waiting to go
    (`qbo_edit_requested_at`, APC-109): otherwise the hourly run never updates one (a mismatch
    would rewrite it every hour); only a person's Retry sends it again."""
    rows = (
        Invoice.objects.filter(status=Invoice.Status.OPEN)
        .filter(_stale("qbo_", realm))
        .exclude(group__qbo_id="")
        .filter(group__qbo_realm_id=realm)
        .filter(Exists(InvoiceLine.objects.filter(invoice=OuterRef("pk"))))
    )
    if not linked:
        # `_stale` already holds a row linked here to NOT_SYNCED / ERROR.
        rows = rows.filter(
            Q(qbo_id="") | ~Q(qbo_realm_id=realm) | Q(qbo_edit_requested_at__isnull=False)
        )
    if start is not None:
        rows = rows.filter(invoice_date__gte=start)
    return unsent_first(rows.select_related("group"))


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


def _linked_here(record, realm: str) -> bool:
    return bool(record.qbo_id) and record.qbo_realm_id == realm


def _push_invoice(invoice: Invoice, realm: str) -> bool:
    """Create (or update) the QuickBooks Invoice; QuickBooks numbers it and the number comes
    back onto ours. One QuickBooks holds differently (sales tax on the item, most likely) is
    ERROR with both figures and alerts once; the hourly run never sends it again.

    An update (an edit, APC-109) goes sparse with its SyncToken, never while its date or the
    date QuickBooks holds is in a closed period: that is ERROR with no call. A create is
    QuickBooks' call whatever its date. A create that lands holding other values than ours (an
    edit saved while it was in flight, or a replay of an earlier attempt) is queued as an
    update."""
    returned: dict = {}
    sent: dict = {}
    mismatch = ""
    already_failed = invoice.qbo_sync_state == SyncState.ERROR
    stamp = invoice.qbo_edit_requested_at
    # Sparse touches updates only: a create sends no Id, so `_send` adds no flag.
    link = _Link(invoice, "invoice", "Invoice", sparse=True)
    creating = not _linked_here(invoice, realm)
    if not creating and closed_in_quickbooks(invoice):
        _fail(link, CLOSED_UPDATE_MESSAGE)
        return False

    def matches(obj: dict, body: dict) -> bool:
        nonlocal mismatch
        returned.update(obj)
        sent.update(body)
        if invoice_matches(obj, body):
            return True
        mismatch = _mismatch(obj, body, invoice)
        return False

    in_step = _push(link, lambda: invoice_payload(invoice, realm), matches=matches, what="invoice")
    # Whatever came back is the invoice QuickBooks holds, so its number is ours to show.
    number = str(returned.get("DocNumber") or "")[:DOC_NUMBER_MAX]
    if number and number != invoice.number:
        Invoice.objects.filter(pk=invoice.pk).update(number=number)
        invoice.number = number
    held = _txn_date(returned)
    if held is not None and held != invoice.qbo_txn_date:
        Invoice.objects.filter(pk=invoice.pk).update(qbo_txn_date=held)
        invoice.qbo_txn_date = held
    # Voided while QuickBooks was making it: the void follows it there, whatever it totals.
    linked = invoice.qbo_id and invoice.status == Invoice.Status.OPEN
    if mismatch and linked and invoice.qbo_sync_state == SyncState.NOT_SYNCED:
        _fail(link, mismatch, alert=not already_failed)
        # The edit did land; only a person's Retry sends it again. An edit saved during the
        # push wrote a newer stamp, and that one still goes.
        Invoice.objects.filter(pk=invoice.pk, qbo_edit_requested_at=stamp).update(
            qbo_edit_requested_at=None
        )
        invoice.refresh_from_db(fields=["qbo_edit_requested_at"])
    elif creating and linked and returned and _stale_values(returned, sent):
        # A replay of an earlier attempt, made before our last edit: the edit follows.
        Invoice.objects.filter(pk=invoice.pk).update(
            qbo_sync_state=SyncState.NOT_SYNCED, qbo_sync_error=""
        )
        _queue_update(invoice)
        return False
    elif in_step:
        # Only while still SYNCED: an edit committing now sets NOT_SYNCED and a new stamp.
        Invoice.objects.filter(pk=invoice.pk, qbo_sync_state=SyncState.SYNCED).exclude(
            qbo_edit_requested_at=None
        ).update(qbo_edit_requested_at=None)
        invoice.refresh_from_db(fields=["qbo_edit_requested_at"])
    elif creating and linked and invoice.qbo_sync_state == SyncState.NOT_SYNCED:
        # Landed, but changed here while QuickBooks made it: the change goes as an update.
        _queue_update(invoice)
    return in_step


def _txn_date(obj: dict) -> date | None:
    """The date QuickBooks holds an invoice under, from its answer (None when absent or odd)."""
    raw = obj.get("TxnDate")
    try:
        return parse_date(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _stale_values(obj: dict, body: dict) -> bool:
    """QuickBooks answered a create with another date or note than we sent: it replayed an
    attempt made before an edit."""
    return obj.get("TxnDate") != body.get("TxnDate") or obj.get("PrivateNote", "") != body.get(
        "PrivateNote", ""
    )


def _queue_update(invoice: Invoice) -> None:
    """Stamp a landed invoice NOT_SYNCED with no stamp yet so the next run sends it as an
    update."""
    Invoice.objects.filter(
        pk=invoice.pk, qbo_sync_state=SyncState.NOT_SYNCED, qbo_edit_requested_at=None
    ).update(qbo_edit_requested_at=timezone.now())
    invoice.refresh_from_db(fields=["qbo_sync_state", "qbo_sync_error", "qbo_edit_requested_at"])


def push_invoice(invoice: Invoice) -> bool:
    """Send one invoice to QuickBooks, holding its lease. True when it is now in step; False
    with no call while another push has it, or while it isn't one to send (void, before the
    start date, its group not in QuickBooks yet). Raises SyncPaused / QboNotConnected."""
    with _leased(Invoice, invoice.pk, "qbo_push_started_at") as mine:
        if not mine:
            return False
        realm = current_realm()
        start = customer_docs_from()
        _clear_blocks(start, Invoice.objects.filter(pk=invoice.pk))
        invoice.refresh_from_db()
        if not pending_invoices(realm, start, linked=True).filter(pk=invoice.pk).exists():
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
    """Void the QuickBooks invoice, never while its date or the date QuickBooks holds is in a
    closed period: that is ERROR with no call."""
    link = _Link(invoice, "invoice", "Invoice")
    if closed_in_quickbooks(invoice):
        _fail(link, CLOSED_PERIOD_MESSAGE)
        return False
    try:
        obj = _void(invoice)
    except qbo.QboAPIError as exc:
        logger.warning("QuickBooks refused void of invoice %s: %s", invoice.pk, exc)
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


def _clear_blocks(start: date | None, rows=None) -> None:
    """A block whose setting has since been chosen no longer applies: back to not sent, so an
    invoice now before the start date says nothing and the next real refusal alerts."""
    cleared = []
    if start is not None:
        cleared.append(NO_START_DATE)
    if trip_item_ref() is not None:
        cleared.append(NO_TRIP_ITEM)
    if not cleared:
        return
    rows = Invoice.objects.all() if rows is None else rows
    rows.filter(
        status=Invoice.Status.OPEN, qbo_sync_state=SyncState.ERROR, qbo_sync_error__in=cleared
    ).update(qbo_sync_state=SyncState.NOT_SYNCED, qbo_sync_error="")


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


def sync_pending(budget: PushBudget | None = None) -> int:
    """Push up to SYNC_BATCH invoices and voids that aren't in step: creates (not yet sent
    first), then voids (a void never needs the start date or the item), starting none once
    `budget` has run out (None: no time limit). Returns how many landed. Stops quietly on an
    outage, a rate limit or a lost connection; nothing is marked."""
    if not qbo.is_connected():
        return 0
    realm = current_realm()
    start = customer_docs_from()
    pushed = attempted = 0
    blocked = ""  # newly blocked by a missing setting: alert once for the run
    _clear_blocks(start)
    try:
        rows = list(pending_invoices(realm, start))
        if rows and start is None:
            blocked = NO_START_DATE if _block_all(rows, NO_START_DATE) else ""
        elif rows and trip_item_ref() is None:
            blocked = NO_TRIP_ITEM if _block_all(rows, NO_TRIP_ITEM) else ""
        else:
            for invoice in rows:
                if attempted >= SYNC_BATCH or (budget and budget.expired()):
                    return pushed
                attempted += 1
                pushed += push_invoice(invoice)
        for invoice in pending_voids(realm):
            if attempted >= SYNC_BATCH or (budget and budget.expired()):
                return pushed
            attempted += 1
            pushed += push_void(invoice)
    except (SyncPaused, qbo.QboNotConnected) as exc:
        logger.warning("QuickBooks invoice sync paused: %s", exc)
    finally:
        if blocked:
            _alert_blocked(blocked)
    return pushed


_UNSET = object()


def sync_state(invoice: Invoice, *, connected: bool | None = None, start=_UNSET) -> dict | None:
    """What the invoice row shows: its state, its sentence, where Retry posts, whether it is
    dated before the start date (so never sent) and whether it changed here since it was sent.
    None while QuickBooks isn't connected. Pass `connected` and `start` when building many."""
    from django.urls import reverse

    if connected is None:
        connected = qbo.is_connected()
    if not connected:
        return None
    if start is _UNSET:
        start = customer_docs_from()
    return {
        "state": invoice.qbo_sync_state,
        "error": invoice.qbo_sync_error,
        "sync_url": reverse("billing_invoice_sync", args=[invoice.pk]),
        "before_start": start is not None and invoice.invoice_date < start,
        "changed": changed_since_sent(invoice),
    }


def changed_since_sent(record) -> bool:
    """In QuickBooks, then changed here (NOT_SYNCED with nothing refused): the hourly run
    leaves it, and a person's Retry sends the change."""
    return (
        bool(record.qbo_id)
        and record.qbo_sync_state == SyncState.NOT_SYNCED
        and not record.qbo_sync_error
    )
