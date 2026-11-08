"""Customer Billing (APC-106, spec 2026-10-25 §7): processed trips billed to an account,
gathered into invoices one line per trip. Accounts and groups on the left, the picked
group's ready trips and open invoices on the right.

Reads are open to staff; creating and voiding an invoice need payments access.
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

from . import invoices
from .models import AccountGroup, Invoice, Terms

# An invoice date outside this window is a typo, not a backdated or forward invoice.
EARLIEST_INVOICE_DATE = date(2020, 1, 1)
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
    pane: dict = {}
    if group is not None:
        trips = invoices.group_ready_trips(group, filters)
        terms = group.effective_terms
        pos = [(t.lead.po_number or "").strip() for t in trips]
        pane = {
            "trips": trips,
            "trip_lines": [(t, invoices.line_description(t)) for t in trips],
            "terms_label": dict(Terms.choices)[terms],
            "term_days": invoices.TERM_DAYS[terms],
            "due_preview": invoices.due_date(today, terms),
            "default_po": pos[0] if pos else "",
            "mixed_po": len({po for po in pos if po}) > 1,
            "invoices": invoices.open_invoices(group),
            "draft": {
                "trips": [
                    {"id": t.pk, "amount": f"{t.line_total:.2f}", "po": po}
                    for t, po in zip(trips, pos, strict=True)
                ],
                "termDays": invoices.TERM_DAYS[terms],
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
    kept = _filter_params(invoices.BillingFilters.from_query(request.POST))
    query = {"group": pk, "invoice": invoice.pk, **{k: v for k, v in kept.items() if v}}
    return JsonResponse({"ok": True, "redirect": f"{reverse('billing')}?{urlencode(query)}"})


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
