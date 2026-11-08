"""Import, export and history endpoints (APC-141, spec §4.5, §9, §10)."""

import html as html_lib
import io
import re
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.http import content_disposition_header
from openpyxl import load_workbook

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.imports import services
from apps.imports.factories import ImportRunFactory
from apps.imports.models import ImportRun
from apps.vendors.factories import VendorFactory

from .helpers import make_xlsx

pytestmark = pytest.mark.django_db

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def admin(client):
    user = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(user)
    return user


def _upload_file(rows=None, name="people.xlsx"):
    rows = rows if rows is not None else [["Ann Lee", "ann@acme.test"]]
    buf = make_xlsx(["Name", "Email"], rows)
    buf.name = name
    return buf


def _previewed(rows=None, screen="contacts", **extra):
    run = ImportRunFactory(screen=screen, filename="people.xlsx", **extra)
    run.file.save("people.xlsx", ContentFile(make_xlsx(["Name", "Email"], rows or []).read()))
    services.preview(run)
    run.refresh_from_db()
    return run


def _urls(run):
    return [
        ("get", reverse("import_history")),
        ("get", reverse("import_template", args=["contacts"])),
        ("get", reverse("import_export", args=["contacts"])),
        ("post", reverse("import_upload", args=["contacts"])),
        ("get", reverse("import_run", args=[run.pk])),
        ("post", reverse("import_confirm", args=[run.pk])),
        ("get", reverse("import_errors", args=[run.pk])),
    ]


def test_every_endpoint_forbids_agents(client):
    run = ImportRunFactory()
    client.force_login(UserFactory(role=User.Role.AGENT))
    for method, url in _urls(run):
        assert getattr(client, method)(url).status_code == 403, url


def test_every_endpoint_blocks_anonymous(client):
    run = ImportRunFactory()
    for method, url in _urls(run):
        assert getattr(client, method)(url).status_code != 200, url


def test_unknown_screen_404s(client, admin):
    assert client.get("/portal/imports/nope/template.xlsx").status_code == 404
    assert client.get("/portal/imports/nope/export.xlsx").status_code == 404
    assert client.post("/portal/imports/nope/upload/").status_code == 404


def test_upload_creates_previewed_run_and_returns_url(client, admin):
    response = client.post(reverse("import_upload", args=["contacts"]), {"file": _upload_file()})
    run = ImportRun.objects.get()
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": reverse("import_run", args=[run.pk])}
    assert (run.kind, run.status, run.user) == ("import", "previewed", admin)
    assert run.filename == "people.xlsx"
    assert not Contact.objects.filter(email="ann@acme.test").exists()


def test_upload_file_error_returns_sentence_and_keeps_failed_run(client, admin):
    response = client.post(
        reverse("import_upload", args=["contacts"]), {"file": _upload_file(rows=[])}
    )
    run = ImportRun.objects.get()
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": services.NO_ROWS}
    assert run.status == "failed"


def test_upload_without_a_file_creates_no_run(client, admin):
    response = client.post(reverse("import_upload", args=["contacts"]))
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Choose a .xlsx file to upload."}
    assert ImportRun.objects.count() == 0


def test_upload_that_is_not_xlsx_keeps_a_failed_run_without_the_file(client, admin):
    response = client.post(
        reverse("import_upload", args=["contacts"]), {"file": _upload_file(name="people.csv")}
    )
    run = ImportRun.objects.get()
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": run.error}
    assert run.error == "This isn't an Excel file we can read. Upload a .xlsx file."
    assert (run.kind, run.status, run.screen, run.user) == ("import", "failed", "contacts", admin)
    assert run.filename == "people.csv"
    assert not run.file


def test_upload_over_the_size_limit_is_refused_before_storing(client, admin, monkeypatch):
    monkeypatch.setattr(services, "MAX_BYTES", 10)
    stored = []
    monkeypatch.setattr(
        "django.core.files.storage.FileSystemStorage._save",
        lambda self, name, content: stored.append(name) or name,
    )
    response = client.post(reverse("import_upload", args=["contacts"]), {"file": _upload_file()})
    run = ImportRun.objects.get()
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": services.TOO_BIG}
    assert (run.status, run.error, run.filename) == ("failed", services.TOO_BIG, "people.xlsx")
    assert not run.file
    assert stored == []


def test_run_page_lists_errors_before_new_and_updated(client, admin):
    Contact.objects.create(name="Old Name", email="bob@acme.test")
    run = _previewed(
        [["Ann Lee", "ann@acme.test"], ["Robert", "bob@acme.test"], ["", "bad@acme.test"]]
    )
    response = client.get(reverse("import_run", args=[run.pk]))
    html = response.content.decode()
    ctx = response.context
    assert len(ctx["errors"]) == 1
    assert len(ctx["new"]) == 1
    assert len(ctx["updated"]) == 1
    assert ctx["applicable"] == 2
    assert ctx["expired"] is False
    assert ctx["nav"] == "contacts"
    sections = re.findall(r'data-section="(\w+)"', html)
    assert sections == ["errors", "new", "updated"]


def test_confirm_applies_and_redirects(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"]])
    response = client.post(reverse("import_confirm", args=[run.pk]), follow=True)
    assert response.redirect_chain[-1][0] == reverse("import_run", args=[run.pk])
    assert [str(m) for m in response.context["messages"]] == ["Applied: 1 new · 0 updated."]
    assert Contact.objects.filter(email="ann@acme.test").exists()


def test_second_confirm_refused(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"]])
    url = reverse("import_confirm", args=[run.pk])
    client.post(url, follow=True)
    response = client.post(url, follow=True)
    assert [str(m) for m in response.context["messages"]] == ["This import was already applied."]


def test_expired_preview_refused(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"]])
    ImportRun.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - services.PREVIEW_TTL - services.PREVIEW_TTL
    )
    response = client.post(reverse("import_confirm", args=[run.pk]), follow=True)
    assert [str(m) for m in response.context["messages"]] == [services.EXPIRED]
    assert not Contact.objects.filter(email="ann@acme.test").exists()


def test_errors_download(client, admin):
    run = _previewed([["", "bad@acme.test"], ["Ann Lee", "ann@acme.test"]])
    response = client.get(reverse("import_errors", args=[run.pk]))
    assert response["Content-Type"] == XLSX
    assert response["Content-Disposition"] == 'attachment; filename="people-errors.xlsx"'
    sheet = load_workbook(io.BytesIO(response.content))["Contacts"]
    header = [c.value for c in sheet[1]]
    assert header[-1] == "Reason"
    assert sheet.max_row == 2


def test_errors_download_404s_without_error_rows(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"]])
    assert client.get(reverse("import_errors", args=[run.pk])).status_code == 404


def test_history_filters_by_screen(client, admin):
    ImportRunFactory(screen="contacts", filename="c-file.xlsx")
    ImportRunFactory(screen="vendors", filename="v-file.xlsx")
    url = reverse("import_history")
    both = client.get(url).content.decode()
    only = client.get(url, {"screen": "contacts"}).content.decode()
    assert "c-file.xlsx" in both and "v-file.xlsx" in both
    assert "c-file.xlsx" in only and "v-file.xlsx" not in only


def test_history_empty_state(client, admin):
    assert "No imports yet." in client.get(reverse("import_history")).content.decode()


def test_export_download_logs_run(client, admin):
    Contact.objects.create(name="Ann Lee", email="ann@acme.test")
    response = client.get(reverse("import_export", args=["contacts"]), {"scope": "all", "q": "ann"})
    run = ImportRun.objects.get()
    assert response["Content-Type"] == XLSX
    assert response["Content-Disposition"] == f'attachment; filename="{run.filename}"'
    assert (run.kind, run.user, run.counts) == ("export", admin, {"rows": 1})
    assert run.filters == {"scope": "all", "q": "ann"}


def test_template_download(client, admin):
    response = client.get(reverse("import_template", args=["vendors"]))
    assert response["Content-Type"] == XLSX
    assert response["Content-Disposition"] == 'attachment; filename="vendors-template.xlsx"'
    assert "Vendors" in load_workbook(io.BytesIO(response.content)).sheetnames
    assert ImportRun.objects.count() == 0


def _errors_run(filename):
    run = ImportRunFactory(screen="contacts", filename=filename)
    run.file.save(
        "people.xlsx", ContentFile(make_xlsx(["Name", "Email"], [["", "x@acme.test"]]).read())
    )
    services.preview(run)
    return run


@pytest.mark.parametrize(
    ("filename", "download"),
    [
        ('Ann "Q" list.xlsx', 'Ann "Q" list-errors.xlsx'),
        ("Café list.xlsx", "Café list-errors.xlsx"),
    ],
)
def test_errors_download_quotes_any_upload_name(client, admin, filename, download):
    run = _errors_run(filename)
    response = client.get(reverse("import_errors", args=[run.pk]))
    assert response["Content-Disposition"] == content_disposition_header(True, download)


# --- the Excel menu on the two list screens -----------------------------------------


@pytest.mark.parametrize("url_name", ["contact_list", "vendor_list"])
def test_excel_menu_only_for_owner_admins(client, url_name):
    agent = UserFactory(role=User.Role.AGENT)
    client.force_login(agent)
    assert "Import from Excel" not in client.get(reverse(url_name)).content.decode()
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    body = client.get(reverse(url_name)).content.decode()
    assert "Import from Excel" in body
    assert "Download template" in body
    assert "Import history" in body


def _href(body, url):
    match = re.search(rf'href="({re.escape(url)}[^"]*)"', body)
    assert match, url
    return html_lib.unescape(match.group(1))


def test_export_link_carries_current_filters(client, admin):
    Contact.objects.create(name="Acme Ann", email="ann@acme.test")
    Contact.objects.create(name="Bob Other", email="bob@other.test")
    body = client.get(reverse("contact_list"), {"scope": "all", "q": "acme"}).content.decode()
    href = _href(body, reverse("import_export", args=["contacts"]))
    assert "scope=all&q=acme" in href
    assert "Export these 1" in body
    assert f'href="{reverse("import_history")}?screen=contacts"' in body


def test_vendor_export_link_carries_status_and_count(client, admin):
    VendorFactory(name="Acme Limo")
    VendorFactory(name="Other Coach")
    body = client.get(reverse("vendor_list"), {"status": "all", "q": "acme"}).content.decode()
    href = _href(body, reverse("import_export", args=["vendors"]))
    assert "status=all&q=acme" in href
    assert "Export these 1" in body


def test_excel_menu_controls_never_submit_the_contacts_search_form(client, admin):
    body = client.get(reverse("contact_list")).content.decode()
    form = re.search(r'<form method="get".*?</form>', body, re.S).group(0)
    assert "Import from Excel" in form
    for tag in re.findall(r"<button[^>]*>", form):
        assert 'type="button"' in tag, tag


def test_upload_modal_body_holds_the_file_input(client, admin):
    body = client.get(reverse("vendor_list")).content.decode()
    block = re.search(r'<template id="import-modal-body-vendors">(.*?)</template>', body, re.S)
    assert block
    assert 'id="import-file"' in block.group(1)
    assert 'accept=".xlsx"' in block.group(1)
    assert reverse("import_template", args=["vendors"]) in block.group(1)


# --- the run page --------------------------------------------------------------------


def _apply_button(body):
    return re.search(r"<button[^>]*data-apply[^>]*>", body).group(0)


def test_run_page_preview_offers_apply(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"], ["", "bad@acme.test"]])
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "disabled" not in _apply_button(body)
    assert "Apply 1 row" in body
    assert "Nothing has been saved yet." in body
    assert reverse("import_confirm", args=[run.pk]) in body
    assert reverse("import_errors", args=[run.pk]) in body
    assert "ann@acme.test" in body  # the New section's Email column


def test_run_page_apply_disabled_when_expired(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"]])
    ImportRun.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - services.PREVIEW_TTL - services.PREVIEW_TTL
    )
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "disabled" in _apply_button(body)
    assert services.EXPIRED in body
    assert "Nothing has been saved yet." not in body


def test_run_page_apply_disabled_when_nothing_applies(client, admin):
    run = _previewed([["", "bad@acme.test"]])
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "disabled" in _apply_button(body)
    assert reverse("import_errors", args=[run.pk]) in body


def test_run_page_flags_differing_rows(client, admin):
    run = _previewed([["Ann Lee", "ann@acme.test"], ["Bob Lee", "bob@acme.test"]])
    Contact.objects.create(name="Ann Lee", email="ann@acme.test")
    services.apply(run.pk)
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "data-apply" not in body
    assert "Applied " in body and " by " in body
    assert (
        "1 row came out differently from the preview — someone changed these records in between."
        in body
    )


def test_run_page_failed_run_shows_its_error(client, admin):
    run = ImportRunFactory(status=ImportRun.Status.FAILED, error="This file has no rows to import.")
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "This file has no rows to import." in body
    assert "data-apply" not in body


def test_run_page_failed_during_apply_shows_only_the_error(client, admin):
    """A run that previewed, then failed on apply, still holds its preview rows: the page
    shows the error alone, never the sections or the action bar."""
    Contact.objects.create(name="Bob", email="bob@acme.test")
    run = _previewed(
        [["Ann Lee", "ann@acme.test"], ["Robert", "bob@acme.test"], ["", "bad@acme.test"]]
    )
    ImportRun.objects.filter(pk=run.pk).update(
        status=ImportRun.Status.FAILED, error="Something went wrong reading row 3."
    )
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert "Something went wrong reading row 3." in body
    assert "Nothing was saved." in body
    assert "data-section" not in body
    assert "data-apply" not in body
    assert reverse("import_errors", args=[run.pk]) not in body


def test_run_page_failed_error_already_saying_nothing_was_saved_says_it_once(client, admin):
    run = ImportRunFactory(status=ImportRun.Status.FAILED, error=services.FILE_CRASHED)
    body = client.get(reverse("import_run", args=[run.pk])).content.decode()
    assert body.count("Nothing was saved.") == 1


# --- the history page ----------------------------------------------------------------


def test_history_uses_searchable_select(client, admin):
    body = client.get(reverse("import_history"), {"screen": "vendors"}).content.decode()
    selects = body.split("<select")[1:]
    assert selects
    for chunk in selects:
        assert "data-tom" in chunk.split(">")[0]
    assert 'name="screen"' in body
    assert '<option value="vendors" selected>Vendors</option>' in body
    assert "All screens" in body


def test_history_spells_results_and_chips(client, admin):
    ImportRunFactory(
        kind=ImportRun.Kind.EXPORT,
        status=ImportRun.Status.APPLIED,
        filename="contacts-2026-11-08.xlsx",
        filters={"scope": "customers", "q": "acme"},
        counts={"rows": 1204},
    )
    ImportRunFactory(
        kind=ImportRun.Kind.EXPORT,
        status=ImportRun.Status.APPLIED,
        screen="vendors",
        filename="vendors-2026-11-08.xlsx",
        filters={"status": "inactive", "q": ""},
        counts={"rows": 3},
    )
    applied = ImportRunFactory(
        status=ImportRun.Status.APPLIED,
        counts={"new": 40, "updated": 310, "unchanged": 2, "errors": 12, "examples": 0},
    )
    preview = ImportRunFactory(
        preview_counts={"new": 1, "updated": 0, "unchanged": 0, "errors": 0, "examples": 0}
    )
    ImportRunFactory(status=ImportRun.Status.FAILED, error="This file has no rows to import.")
    body = client.get(reverse("import_history")).content.decode()
    assert "Exported 1,204 contacts — Customers, search &#x27;acme&#x27;" in body
    assert "Exported 3 vendors — Archived" in body
    assert "40 new · 310 updated · 12 errors" in body
    assert "Preview — not applied" in body
    assert "Failed" in body
    assert reverse("import_run", args=[applied.pk]) in body
    assert reverse("import_run", args=[preview.pk]) in body
    exports = ImportRun.objects.filter(kind=ImportRun.Kind.EXPORT)
    for run in exports:
        assert reverse("import_run", args=[run.pk]) not in body


def test_history_marks_expired_previews(client, admin):
    run = ImportRunFactory(preview_counts={"new": 1, "updated": 0, "errors": 0})
    ImportRun.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - services.PREVIEW_TTL - services.PREVIEW_TTL
    )
    body = client.get(reverse("import_history")).content.decode()
    assert "Expired" in body
    assert "Preview — not applied" not in body


def test_history_never_loads_the_rows_json(client, admin):
    ImportRunFactory(
        status=ImportRun.Status.APPLIED,
        counts={"new": 2, "updated": 0, "unchanged": 0, "errors": 0, "examples": 0},
        rows=[{"row": 2, "outcome": "new"}],
    )
    with CaptureQueriesContext(connection) as queries:
        body = client.get(reverse("import_history")).content.decode()
    assert "2 new" in body
    run_selects = [q["sql"] for q in queries if "imports_importrun" in q["sql"]]
    assert run_selects
    assert all("`rows`" not in sql and '"rows"' not in sql for sql in run_selects)


# --- the upload handler (static/js/app.js) ---------------------------------------------


def _upload_js() -> str:
    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index("function uploadProblem")
    return js[start : js.index("/* ----", start)]


def test_upload_handler_explains_a_response_that_is_not_json():
    body = _upload_js()
    assert "content-type" in body.lower()
    assert "resp.redirected" in body and "/login/" in body
    assert "Your session has ended. Sign in again." in body
    assert "resp.status === 403" in body
    assert "You don't have access to imports" in body
    assert "Sign in again as an admin." in body
    assert "resp.status >= 500" in body
    assert "This is taking longer than expected" in body
    assert (
        "Large files can take a while. Check Import history in a minute — the preview will be "
        "there if it finished." in body
    )
    assert "window.alert" not in body and "confirm(" not in body
    assert "return new Promise(() => {});" in body  # an alert stays up until its own button
