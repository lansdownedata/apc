"""Customer Billing (APC-106, spec 2026-10-25 §7): processed trips billed to an account,
gathered into invoices one line per trip. Accounts and groups on the left, the picked
group's ready trips and open invoices on the right.

Reads are open to staff; creating, editing and voiding an invoice need payments access.
"""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import payment_access_required
from apps.integrations import qbo

from . import invoices, qbo_invoices
from .invoices import EARLIEST_INVOICE_DATE
from .models import AccountGroup, Invoice, QboAccountMapping, Terms
from .qbo_sync import SyncPaused

# An invoice date outside this window is a typo, not a backdated or forward invoice.
LATEST_INVOICE_DAYS_AHEAD = 365


def _bad(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"ok": False, "error": message}, status=status)


def _filter_params(filters: invoices.BillingFilters) -> dict[str, str]:
    """The page's filters as query-string values, blank ones included."""
    return {
        "q": filters.q,
        "from": filters.date_from.isoformat() if filters.date_from else "",
        "to": filters.date_to.isoformat() if filters.date_to else "",
    }


def _with_sync(rows: list[Invoice], *, connected: bool, start) -> list[Invoice]:
    """Attach each invoice's QuickBooks row state as `.sync` (None while disconnected)."""
    for invoice in rows:
        invoice.sync = qbo_invoices.sync_state(invoice, connected=connected, start=start)
    return rows


def _with_corrections(rows: list[Invoice], config: QboAccountMapping) -> None:
    """Attach `.closed` (in a closed period: read-only on the row) and `.replaces` (the invoice
    this one was issued in place of, or None) to each row, in one query for all of them."""
    replaced = (
        {old.replaced_by_id: old for old in Invoice.objects.filter(replaced_by__in=rows)}
        if rows
        else {}
    )
    for invoice in rows:
        invoice.closed = invoices.closed_in_quickbooks(invoice, config)
        invoice.replaces = replaced.get(invoice.pk)


def _billing_url(group_id: int, invoice_id: int, request: HttpRequest) -> str:
    """The Billing page on `group`, highlighting `invoice`, keeping the page's filters."""
    kept = _filter_params(invoices.BillingFilters.from_query(request.POST))
    query = {"group": group_id, "invoice": invoice_id, **{k: v for k, v in kept.items() if v}}
    return f"{reverse('billing')}?{urlencode(query)}"


@login_required
@require_GET
def billing_list(request: HttpRequest) -> HttpResponse:
    filters = invoices.BillingFilters.from_query(request.GET)
    accounts = invoices.ready_accounts(filters)
    raw = request.GET.get("group", "")
    if raw.isdigit():
        group = get_object_or_404(AccountGroup.objects.select_related("account"), pk=int(raw))
    else:
        group = accounts[0].groups[0].group if accounts else None
    today = timezone.localdate()
    # Read, never created on a GET: no settings row yet means no closing date.
    config = QboAccountMapping.objects.filter(pk=1).first() or QboAccountMapping()
    pane: dict = {}
    if group is not None:
        trips = invoices.group_ready_trips(group, filters)
        terms = group.effective_terms
        pos = [(t.lead.po_number or "").strip() for t in trips]
        connected = qbo.is_connected()
        start = qbo_invoices.customer_docs_from() if connected else None
        synced = {"connected": connected, "start": start}
        open_rows = _with_sync(invoices.open_invoices(group), **synced)
        # Only while connected: a void can't reach QuickBooks without the connection.
        waiting = _with_sync(invoices.voids_waiting(group), **synced) if connected else []
        _with_corrections(open_rows + waiting, config)
        pane = {
            "trips": trips,
            "trip_lines": [(t, invoices.line_description(t)) for t in trips],
            "terms_label": dict(Terms.choices)[terms],
            "term_days": invoices.TERM_DAYS[terms],
            "due_preview": invoices.due_date(today, terms),
            "default_po": pos[0] if pos else "",
            "mixed_po": len({po for po in pos if po}) > 1,
            "invoices": open_rows,
            "voids_waiting": waiting,
            "draft": {
                "trips": [
                    {"id": t.pk, "amount": f"{t.line_total:.2f}", "po": po}
                    for t, po in zip(trips, pos, strict=True)
                ],
                "termDays": invoices.TERM_DAYS[terms],
                "today": today.isoformat(),
                "group": group.name,
                "filters": _filter_params(filters),
            },
        }
    raw_invoice = request.GET.get("invoice", "")
    return render(
        request,
        "billing/list.html",
        {
            "nav": "billing",
            "page_title": "Customer Billing",
            "filters": filters,
            "accounts": accounts,
            "group": group,
            "trips": [],
            "invoices": [],
            "today": today,
            "ready_count": sum(len(g.trips) for a in accounts for g in a.groups),
            "highlight": int(raw_invoice) if raw_invoice.isdigit() else None,
            "book_close_date": config.book_close_date,
            **pane,
        },
    )


@login_required
@payment_access_required
@require_POST
def invoice_create(request: HttpRequest, pk: int) -> JsonResponse:
    group = get_object_or_404(AccountGroup.objects.select_related("account"), pk=pk)
    try:
        invoice_date = date.fromisoformat(request.POST.get("invoice_date", ""))
    except ValueError:
        return _bad("Enter a valid invoice date.")
    latest = timezone.localdate() + timedelta(days=LATEST_INVOICE_DAYS_AHEAD)
    if not EARLIEST_INVOICE_DATE <= invoice_date <= latest:
        return _bad("Enter a valid invoice date.")
    trip_ids = [int(raw) for raw in request.POST.getlist("trip_ids") if raw.isdigit()]
    try:
        invoice = invoices.create_invoice(
            group,
            trip_ids,
            invoice_date=invoice_date,
            po_number=request.POST.get("po_number", ""),
            memo=request.POST.get("memo", ""),
            user=request.user,
        )
    except invoices.InvoiceError as exc:
        return _bad(str(exc))
    # The page's filters ride along so the reload lands on the same list.
    return JsonResponse({"ok": True, "redirect": _billing_url(pk, invoice.pk, request)})


@login_required
@payment_access_required
@require_POST
def invoice_void(request: HttpRequest, pk: int) -> JsonResponse:
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        invoices.void_invoice(invoice, reason=request.POST.get("reason", ""), user=request.user)
    except invoices.InvoiceError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True})


@login_required
@payment_access_required
@require_POST
def invoice_edit(request: HttpRequest, pk: int) -> JsonResponse:
    """Correct an open invoice's date, PO and memo (APC-109)."""
    invoice = get_object_or_404(Invoice, pk=pk)
    try:
        invoice_date = date.fromisoformat(request.POST.get("invoice_date", ""))
    except ValueError:
        return _bad("Enter a valid invoice date.")
    try:
        invoice = invoices.edit_invoice(
            invoice,
            invoice_date=invoice_date,
            po_number=request.POST.get("po_number", ""),
            memo=request.POST.get("memo", ""),
            user=request.user,
        )
    except invoices.InvoiceError as exc:
        return _bad(str(exc))
    return JsonResponse(
        {"ok": True, "redirect": _billing_url(invoice.group_id, invoice.pk, request)}
    )


@login_required
@payment_access_required
@require_POST
def invoice_replace(request: HttpRequest, pk: int) -> JsonResponse:
    """Void an open invoice and issue a new one from the same trips; the reload highlights
    the new one (APC-109). The new date defaults to today."""
    invoice = get_object_or_404(Invoice, pk=pk)
    raw_date = request.POST.get("invoice_date", "").strip()
    try:
        invoice_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
    except ValueError:
        return _bad("Enter a valid invoice date.")
    try:
        new = invoices.void_and_replace(
            invoice,
            reason=request.POST.get("reason", ""),
            invoice_date=invoice_date,
            po_number=request.POST.get("po_number", ""),
            memo=request.POST.get("memo", ""),
            user=request.user,
        )
    except invoices.InvoiceError as exc:
        return _bad(str(exc))
    return JsonResponse({"ok": True, "redirect": _billing_url(new.group_id, new.pk, request)})


@login_required
@payment_access_required
@require_POST
def invoice_sync(request: HttpRequest, pk: int) -> JsonResponse:
    """Retry one invoice's QuickBooks push now; a person asked, so this call may wait. Judged
    on the closing date QuickBooks has now (APC-109)."""
    invoice = get_object_or_404(Invoice, pk=pk)
    if not qbo.is_connected():
        return _bad("QuickBooks isn't connected.")
    qbo_invoices.refresh_preferences_quietly()
    push = (
        qbo_invoices.push_void
        if invoice.status == Invoice.Status.VOID
        else (qbo_invoices.push_invoice)
    )
    try:
        push(invoice)
    except qbo.QboNotConnected as exc:
        return _bad(str(exc))
    except SyncPaused:
        return _bad("QuickBooks couldn't be reached. Try again in a minute.", status=502)
    invoice.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "state": invoice.qbo_sync_state,
            "error": invoice.qbo_sync_error,
            "number": invoice.number,
        }
    )
