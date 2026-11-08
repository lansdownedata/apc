"""Import, export and history endpoints (APC-141, spec §4.5, §9, §10). Owner-admin only."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import owner_admin_required

from . import services
from .models import ImportRun
from .screens import SCREENS, get_screen

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAGE_SIZE = 50
NEED_XLSX = "Choose a .xlsx file to upload."


def _xlsx(content: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(content, content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@owner_admin_required
@require_GET
def history(request: HttpRequest) -> HttpResponse:
    screen_filter = request.GET.get("screen", "")
    runs = ImportRun.objects.select_related("user")
    if screen_filter in SCREENS:
        runs = runs.filter(screen=screen_filter)
    else:
        screen_filter = ""
    page_obj = Paginator(runs, PAGE_SIZE).get_page(request.GET.get("page"))
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
    found = get_screen(screen)
    file = request.FILES.get("file")
    if file is None or not file.name.lower().endswith(".xlsx"):
        return JsonResponse({"ok": False, "error": NEED_XLSX}, status=400)
    run = ImportRun.objects.create(
        kind=ImportRun.Kind.IMPORT,
        status=ImportRun.Status.PREVIEWED,
        screen=found.slug,
        user=request.user,
        filename=file.name[:255],
        file=file,
    )
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
    rows = run.rows
    new = [r for r in rows if r["outcome"] == "new"]
    updated = [r for r in rows if r["outcome"] == "updated"]
    return render(
        request,
        "imports/run_detail.html",
        {
            "run": run,
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
        run = services.apply(pk)
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
