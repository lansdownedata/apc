"""Import, export and history endpoints (APC-141, spec §4.5, §9, §10)."""

import io

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.models import Contact
from apps.imports import services
from apps.imports.factories import ImportRunFactory
from apps.imports.models import ImportRun

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


def test_upload_without_xlsx_creates_no_run(client, admin):
    url = reverse("import_upload", args=["contacts"])
    expected = {"ok": False, "error": "Choose a .xlsx file to upload."}
    missing = client.post(url)
    wrong = client.post(url, {"file": _upload_file(name="people.csv")})
    assert (missing.status_code, missing.json()) == (400, expected)
    assert (wrong.status_code, wrong.json()) == (400, expected)
    assert ImportRun.objects.count() == 0


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
    assert html.index("Errors") < html.index("New") < html.index("Updated")


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
