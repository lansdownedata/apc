"""Import, export and history endpoints (APC-141, spec §4.5, §9, §10). Owner-admin only."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import owner_admin_required

from . import services
from .models import ImportRun
from .screens import SCREENS, get_screen
from .xlsx import NOT_XLSX

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAGE_SIZE = 50
NEED_XLSX = "Choose a .xlsx file to upload."


# How an export's filters read on the history page (spec §4.3).
FILTER_LABELS = {
    "scope": {"customers": "Customers", "all": "All contacts"},
    "status": {"active": "Active", "inactive": "Archived", "all": "All"},
}


def _xlsx(content: bytes, filename: str) -> HttpResponse:
    """A workbook download; the name is quoted (or RFC 5987-encoded) whatever was uploaded."""
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = content_disposition_header(True, filename)
    return response


def _plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def _result(run: ImportRun) -> str:
    """The history page's Result cell: what an import did (or would do), or what an export
    pulled and through which filters — "Exported 1,204 contacts — Customers, search 'acme'"."""
    if run.kind == ImportRun.Kind.EXPORT:
        noun = get_screen(run.screen).noun.removesuffix("s")
        text = f"Exported {_plural(run.counts.get('rows', 0), noun)}"
        parts = [
            labels.get(run.filters[key], run.filters[key])
            for key, labels in FILTER_LABELS.items()
            if run.filters.get(key)
        ]
        if run.filters.get("q"):
            parts.append(f"search '{run.filters['q']}'")
        return f"{text} — {', '.join(parts)}" if parts else text
    if run.status == ImportRun.Status.FAILED:
        return run.error
    counts = run.counts if run.status == ImportRun.Status.APPLIED else run.preview_counts
    parts = [f"{counts.get(k, 0):,} {k}" for k in ("new", "updated") if counts.get(k)]
    if counts.get("errors"):
        parts.append(_plural(counts["errors"], "error"))
    return " · ".join(parts) or "No changes"


def _chip(run: ImportRun) -> str:
    """A status chip only when the run did not apply (spec §9.2)."""
    if run.status == ImportRun.Status.FAILED:
        return "Failed"
    if run.status == ImportRun.Status.PREVIEWED:
        return "Expired" if run.is_expired else "Preview — not applied"
    return ""


def _cell(row: dict, column: str) -> str:
    """A new row's value for `column`, from its changes (a new row lists every set cell)."""
    return next((new for name, _old, new in row.get("changes", []) if name == column), "")


@login_required
@owner_admin_required
@require_GET
def history(request: HttpRequest) -> HttpResponse:
    screen_filter = request.GET.get("screen", "")
    runs = ImportRun.objects.select_related("user").defer("rows")  # the Result reads counts
    if screen_filter in SCREENS:
        runs = runs.filter(screen=screen_filter)
    else:
        screen_filter = ""
    page_obj = Paginator(runs, PAGE_SIZE).get_page(request.GET.get("page"))
    for run in page_obj.object_list:
        run.result, run.chip = _result(run), _chip(run)
    return render(
        request,
        "imports/history.html",
        {
            "page_obj": page_obj,
            "runs": page_obj.object_list,
            "screen_filter": screen_filter,
            "screens": [(slug, screen.noun.title()) for slug, screen in SCREENS.items()],
            "nav": screen_filter or "contacts",
            "page_title": "Import history",
        },
    )


@login_required
@owner_admin_required
@require_GET
def template(request: HttpRequest, screen: str) -> HttpResponse:
    found = get_screen(screen)
    return _xlsx(services.template_bytes(found), f"{found.slug}-template.xlsx")


@login_required
@owner_admin_required
@require_GET
def export(request: HttpRequest, screen: str) -> HttpResponse:
    found = get_screen(screen)
    run, content = services.export_rows(found, request.GET, request.user)
    return _xlsx(content, run.filename)


@login_required
@owner_admin_required
@require_POST
def upload(request: HttpRequest, screen: str) -> JsonResponse:
    """Spec §4.5 step 2. Every upload is a run: a file refused before it is read (not .xlsx,
    too big) is kept as FAILED without storing it; with no file at all there is nothing to log."""
    found = get_screen(screen)
    file = request.FILES.get("file")
    if file is None:
        return JsonResponse({"ok": False, "error": NEED_XLSX}, status=400)
    fields = {
        "kind": ImportRun.Kind.IMPORT,
        "screen": found.slug,
        "user": request.user,
        "filename": file.name[:255],
    }
    refused = ""
    if not file.name.lower().endswith(".xlsx"):
        refused = NOT_XLSX
    elif file.size > services.MAX_BYTES:
        refused = services.TOO_BIG
    if refused:
        ImportRun.objects.create(**fields, status=ImportRun.Status.FAILED, error=refused)
        return JsonResponse({"ok": False, "error": refused}, status=400)
    run = ImportRun.objects.create(**fields, status=ImportRun.Status.PREVIEWED, file=file)
    services.preview(run)
    if run.status == ImportRun.Status.FAILED:
        return JsonResponse({"ok": False, "error": run.error}, status=400)
    return JsonResponse({"ok": True, "url": reverse("import_run", args=[run.pk])})


def _import_run_or_404(pk: int) -> ImportRun:
    return get_object_or_404(ImportRun, pk=pk, kind=ImportRun.Kind.IMPORT)


@login_required
@owner_admin_required
@require_GET
def run_detail(request: HttpRequest, pk: int) -> HttpResponse:
    run = _import_run_or_404(pk)
    # A run that failed (even during apply, after a preview) shows its error alone.
    rows = [] if run.status == ImportRun.Status.FAILED else run.rows
    new = [
        r | {"email": _cell(r, "Email"), "phone": _cell(r, "Phone")}
        for r in rows
        if r["outcome"] == "new"
    ]
    updated = [r for r in rows if r["outcome"] == "updated"]
    return render(
        request,
        "imports/run_detail.html",
        {
            "run": run,
            "counts": run.counts if run.status == ImportRun.Status.APPLIED else run.preview_counts,
            "screen": get_screen(run.screen),
            "errors": [r for r in rows if r["outcome"] == "error"],
            "warnings": [r for r in rows if r.get("warnings")],
            "new": new,
            "updated": updated,
            "unchanged": [r for r in rows if r["outcome"] == "unchanged"],
            "applicable": len(new) + len(updated),
            "differs_count": sum(1 for r in rows if r.get("differs")),
            "expired": run.is_expired,
            "nav": run.screen,
            "page_title": "Import",
        },
    )


@login_required
@owner_admin_required
@require_POST
def confirm(request: HttpRequest, pk: int) -> HttpResponse:
    _import_run_or_404(pk)
    try:
        run = services.apply(pk, user=request.user)
    except services.ApplyRefused as refused:
        messages.error(request, str(refused))
    else:
        if run.status == ImportRun.Status.APPLIED:
            messages.success(
                request, f"Applied: {run.counts['new']} new · {run.counts['updated']} updated."
            )
        else:
            messages.error(request, run.error)
    return redirect("import_run", pk=pk)


@login_required
@owner_admin_required
@require_GET
def errors(request: HttpRequest, pk: int) -> HttpResponse:
    run = _import_run_or_404(pk)
    if not any(row["outcome"] == "error" for row in run.rows):
        raise Http404("No error rows")
    stem = run.filename.rsplit(".", 1)[0] if "." in run.filename else run.filename
    return _xlsx(services.error_rows(run), f"{stem}-errors.xlsx")
