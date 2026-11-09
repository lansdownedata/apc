"""Preview, apply, export, error rows and template (APC-141, spec §3.1, §3.2, §3.4, §4, §7)."""

import io
import logging
from datetime import datetime, timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.functions import Lower
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AddressFactory
from apps.audit.models import AuditEntry
from apps.contacts import selectors as contact_selectors
from apps.contacts import services as contact_services
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact, ContactPhone
from apps.contacts.resources import CONTACT_COLUMNS, ContactResource
from apps.core.choices import Channel
from apps.imports import services
from apps.imports.factories import ImportRunFactory
from apps.imports.models import ImportRun
from apps.imports.resources import row_no
from apps.imports.screens import SCREENS, get_screen
from apps.imports.xlsx import ACCEPTED_SHEET, read_rows
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import VehicleType
from apps.vendors import selectors as vendor_selectors
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor
from apps.vendors.resources import VENDOR_COLUMNS

from .helpers import make_xlsx

pytestmark = pytest.mark.django_db

CONTACT_HEADERS = [c.name for c in CONTACT_COLUMNS]
RULES = [
    "Name is required for new rows.",
    "Rows are matched by Email, else by Phone.",
    "A row with neither an email nor a phone can't be imported.",
    "A blank cell leaves the saved value alone — an import never clears anything.",
    "Separate several values with commas.",
    "Rows with an @example.com email are examples and are skipped.",
]


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _upload(content: bytes, *, screen="contacts", **fields) -> ImportRun:
    return ImportRunFactory(
        screen=screen,
        filename="people.xlsx",
        file=SimpleUploadedFile("people.xlsx", content),
        **fields,
    )


def _sheet(headers, rows, *, screen="contacts") -> bytes:
    return make_xlsx(headers, rows, sheet=screen.title()).getvalue()


def _previewed(headers, rows, *, screen="contacts") -> ImportRun:
    run = _upload(_sheet(headers, rows, screen=screen), screen=screen)
    services.preview(run)
    run.refresh_from_db()
    return run


def _names(content: bytes, screen: str) -> list[str]:
    columns = SCREENS[screen].resource.COLUMNS
    parsed = read_rows(io.BytesIO(content), sheet=screen.title(), columns=columns)
    return [row["Name"] for row in parsed.dataset.dict]


# --- preview (spec §4.5 step 2, §3.4) ---------------------------------------------------


def test_preview_stores_rows_and_counts_errors_first_data():
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    ContactFactory(name="Pat", email="pat@acme.test", phone="")
    run = _previewed(
        ["Name", "Email", "Fax"],
        [
            ["Jordan Smith", "jordan@acme.test", "1"],
            ["Pat", "pat@acme.test", ""],
            ["Sam", "sam@acme.test", ""],
            ["Nobody", "", ""],
            ["Ex Ample", "ex@example.com", ""],
        ],
    )
    assert run.status == ImportRun.Status.PREVIEWED
    assert run.error == ""
    assert run.preview_counts == {
        "new": 1,
        "updated": 1,
        "unchanged": 1,
        "errors": 1,
        "examples": 1,
        "ignored_columns": ["Fax"],
    }
    assert [(r["row"], r["outcome"]) for r in run.rows] == [
        (2, "updated"),
        (3, "unchanged"),
        (4, "new"),
        (5, "error"),
    ]
    assert run.rows[0]["changes"] == [["Name", "Jordan", "Jordan Smith"]]
    assert run.rows[3]["reason"] == "Needs an email or a phone to match or create a contact."
    assert run.counts == {}
    # Nothing written.
    assert Contact.objects.count() == 2
    assert Contact.objects.get(email="jordan@acme.test").name == "Jordan"


def test_preview_merges_reader_row_errors_in_row_order():
    content = make_xlsx(
        ["Name", "Email", "Notes"],
        [
            ["Sam", "sam@acme.test", ""],
            ["Dee", "dee@acme.test", datetime(2026, 1, 2)],
            ["Lee", "lee@acme.test", ""],
        ],
    ).getvalue()
    run = _upload(content)
    services.preview(run)
    run.refresh_from_db()
    assert [(r["row"], r["outcome"]) for r in run.rows] == [(2, "new"), (3, "error"), (4, "new")]
    assert run.rows[1]["reason"] == "Unexpected date in Notes"
    assert run.preview_counts["errors"] == 1


@pytest.mark.parametrize(
    ("limits", "headers", "rows", "error"),
    [
        (
            {"MAX_BYTES": 10},
            ["Name", "Email"],
            [["Sam", "sam@acme.test"]],
            "This file is over 2 MB. Import at most 1,000 rows at a time — split it into "
            "smaller files.",
        ),
        (
            {"MAX_ROWS": 2},
            ["Name", "Email"],
            [["A", "a@acme.test"], ["B", "b@acme.test"], ["C", "c@acme.test"]],
            "This file has 3 rows. Import at most 1,000 at a time — split it into smaller files.",
        ),
        (
            {},
            ["Email"],
            [["a@acme.test"]],
            "This file has no Name column. Use the template's header row.",
        ),
        (
            {},
            ["Name", "Email"],
            [["Ex", "ex@example.com"], ["Ey", "ey@example.org"]],
            "This file has no rows to import.",
        ),
    ],
    ids=["too-big", "too-many-rows", "no-name-column", "only-examples"],
)
def test_preview_file_errors_fail_the_run(monkeypatch, limits, headers, rows, error):
    for name, value in limits.items():
        monkeypatch.setattr(services, name, value)
    run = _previewed(headers, rows)
    assert run.status == ImportRun.Status.FAILED
    assert run.error == error
    assert run.rows == []
    assert not Contact.objects.exists()


def test_preview_of_a_file_that_is_not_xlsx_fails():
    run = _upload(b"name,email\nSam,sam@acme.test\n")
    services.preview(run)
    run.refresh_from_db()
    assert run.status == ImportRun.Status.FAILED
    assert run.error == "This isn't an Excel file we can read. Upload a .xlsx file."


# --- apply (spec §3.1, §4.5 step 3, §7) ----------------------------------------------------


def test_apply_writes_and_records():
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    run = _previewed(
        ["Name", "Email"],
        [["Jordan Smith", "jordan@acme.test"], ["Sam", "sam@acme.test"], ["Nobody", ""]],
    )
    applied = services.apply(run.pk, user=run.user)
    assert applied.pk == run.pk
    run.refresh_from_db()
    assert run.status == ImportRun.Status.APPLIED
    assert run.applied_at is not None
    assert run.counts == {"new": 1, "updated": 1, "unchanged": 0, "errors": 1, "examples": 0}
    sam = Contact.objects.get(email="sam@acme.test")
    assert Contact.objects.get(email="jordan@acme.test").name == "Jordan Smith"
    assert [(r["row"], r["outcome"]) for r in run.rows] == [
        (2, "updated"),
        (3, "new"),
        (4, "error"),
    ]
    assert run.rows[1]["object_id"] == sam.pk
    assert not any(r.get("differs") for r in run.rows)
    # The preview's counts are kept alongside.
    assert run.preview_counts["new"] == 1


def _import_entries(run: ImportRun, model) -> list[AuditEntry]:
    kind = ContentType.objects.get_for_model(model)
    return list(AuditEntry.objects.filter(cid=f"import-{run.pk}", content_type=kind))


def test_apply_credits_whoever_pressed_apply():
    uploader, applier = UserFactory(), UserFactory()
    run = _upload(_sheet(["Name", "Email"], [["Sam", "sam@acme.test"]]), user=uploader)
    services.preview(run)
    services.apply(run.pk, user=applier)
    run.refresh_from_db()
    assert (run.user, run.applied_by) == (uploader, applier)


def test_apply_writes_its_changes_to_the_audit_trail_under_the_run():
    applier = UserFactory()
    jordan = ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    run = _previewed(
        ["Name", "Email", "City"],
        [["Jordan Smith", "jordan@acme.test", "Boston"], ["Sam", "sam@acme.test", ""]],
    )
    assert not _import_entries(run, Contact)  # the dry run's entries roll back with it
    services.apply(run.pk, user=applier)
    entries = _import_entries(run, Contact)
    assert {(e.actor_id, e.source) for e in entries} == {(applier.pk, "import: people.xlsx")}
    jordan_changes = {}
    for entry in entries:
        if entry.object_pk == str(jordan.pk):
            jordan_changes |= entry.changes
    assert jordan_changes["name"] == ["Jordan", "Jordan Smith"]
    assert jordan_changes["primary_address.city"][1] == "Boston"
    sam = Contact.objects.get(email="sam@acme.test")
    assert any(e.object_pk == str(sam.pk) and e.action == AuditEntry.Action.CREATE for e in entries)


def test_vendor_import_logs_its_address_on_the_vendor():
    VendorFactory(name="Acme Limo", email="ops@acme.test")
    run = _previewed(
        ["Name", "Email", "City"], [["Acme Limo", "ops@acme.test", "Denver"]], screen="vendors"
    )
    services.apply(run.pk, user=UserFactory())
    changes = {}
    for entry in _import_entries(run, Vendor):
        changes |= entry.changes
    assert changes["address.city"][1] == "Denver"


def test_apply_revalidates_against_current_data():
    run = _previewed(["Name", "Email"], [["Jordan Smith", "jordan@acme.test"]])
    assert run.rows[0]["outcome"] == "new"
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    services.apply(run.pk, user=run.user)
    run.refresh_from_db()
    row = run.rows[0]
    assert row["outcome"] == "updated"
    assert row["differs"] is True
    assert row["preview_outcome"] == "new"
    assert Contact.objects.count() == 1


@pytest.mark.parametrize(
    ("fields", "age", "message"),
    [
        ({"status": ImportRun.Status.APPLIED}, timedelta(0), "This import was already applied."),
        ({}, timedelta(hours=25), "This preview has expired. Upload the file again."),
        (
            {"status": ImportRun.Status.FAILED},
            timedelta(0),
            "This import failed. Upload the file again.",
        ),
    ],
    ids=["applied", "expired", "failed"],
)
def test_apply_refuses_applied_and_expired(fields, age, message):
    run = _upload(_sheet(["Name", "Email"], [["Sam", "sam@acme.test"]]), **fields)
    ImportRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - age)
    with pytest.raises(services.ApplyRefused) as refused:
        services.apply(run.pk, user=run.user)
    assert str(refused.value) == message
    assert not Contact.objects.exists()
    run.refresh_from_db()
    assert run.status == fields.get("status", ImportRun.Status.PREVIEWED)


def test_preview_of_a_file_gone_from_storage_fails():
    run = _upload(_sheet(["Name", "Email"], [["Sam", "sam@acme.test"]]))
    default_storage.delete(run.file.name)
    services.preview(run)
    run.refresh_from_db()
    assert run.status == ImportRun.Status.FAILED
    assert run.error == "The uploaded file couldn't be found. Upload it again."
    assert not Contact.objects.exists()


@pytest.mark.parametrize("how", ["blanked", "gone-from-storage"])
def test_apply_refuses_when_file_was_purged(how):
    run = _previewed(["Name", "Email"], [["Sam", "sam@acme.test"]])
    if how == "blanked":
        run.file.delete(save=False)
        ImportRun.objects.filter(pk=run.pk).update(file="")
    else:
        default_storage.delete(run.file.name)
    with pytest.raises(services.ApplyRefused) as refused:
        services.apply(run.pk, user=run.user)
    assert str(refused.value) == "This preview has expired. Upload the file again."
    assert not Contact.objects.exists()


def _crash_on_row(monkeypatch, excel_row: int) -> None:
    original = ContactResource.after_save_instance

    def boom(self, instance, row, **kwargs):
        if row_no(row) == excel_row:
            raise RuntimeError("boom")
        return original(self, instance, row, **kwargs)

    monkeypatch.setattr(ContactResource, "after_save_instance", boom)


@pytest.mark.parametrize("stage", ["preview", "apply"])
def test_error_row_crash_fails_run_and_writes_nothing(monkeypatch, caplog, stage):
    ContactFactory(name="Jordan", email="jordan@acme.test", phone="")
    rows = [
        ["Jordan Smith", "jordan@acme.test"],
        ["Sam", "sam@acme.test"],
        ["Lee", "lee@acme.test"],
    ]
    run = _upload(_sheet(["Name", "Email"], rows))
    if stage == "apply":
        services.preview(run)
    _crash_on_row(monkeypatch, 3)
    with caplog.at_level(logging.ERROR, logger="apps.imports.services"):
        if stage == "apply":
            services.apply(run.pk, user=run.user)
        else:
            services.preview(run)
    run.refresh_from_db()
    assert run.status == ImportRun.Status.FAILED
    assert run.error == "Something went wrong reading row 3. Nothing was saved."
    assert Contact.objects.count() == 1
    assert Contact.objects.get().name == "Jordan"
    record = next(r for r in caplog.records if r.name == "apps.imports.services")
    assert record.levelno == logging.ERROR
    assert isinstance(record.exc_info[1], RuntimeError)


def test_an_exception_escaping_the_library_fails_the_run(monkeypatch, caplog):
    run = _previewed(["Name", "Email"], [["Sam", "sam@acme.test"]])

    def explode(self, *args, **kwargs):
        raise RuntimeError("library blew up")

    monkeypatch.setattr(ContactResource, "import_data", explode)
    with caplog.at_level(logging.ERROR, logger="apps.imports.services"):
        services.apply(run.pk, user=run.user)
    run.refresh_from_db()
    assert run.status == ImportRun.Status.FAILED
    assert run.error == "Something went wrong reading this file. Nothing was saved."
    assert not Contact.objects.exists()
    assert any(r.exc_info and r.name == "apps.imports.services" for r in caplog.records)


# --- export (spec §4.2, §4.3) --------------------------------------------------------------


def _export(screen: str, filters: dict, user=None):
    user = user or UserFactory()
    return services.export_rows(get_screen(screen), filters, user)


def test_export_honours_scope_and_q():
    stranger = ContactFactory(name="Stranger", email="stranger@acme.test")
    buyer = ContactFactory(name="buyer Acme", email="buyer@acme.test")
    LeadFactory(contact=buyer)
    LeadFactory(contact=buyer)
    user = UserFactory()

    run, content = _export("contacts", {"scope": "customers", "q": ""}, user)
    assert _names(content, "contacts") == ["buyer Acme"]
    assert run.kind == ImportRun.Kind.EXPORT
    assert run.status == ImportRun.Status.APPLIED
    assert run.screen == ImportRun.Screen.CONTACTS
    assert run.user == user
    assert run.applied_at is not None
    assert run.counts == {"rows": 1}
    assert run.filters == {"scope": "customers", "q": ""}
    assert run.filename == f"contacts-{timezone.localdate():%Y-%m-%d}.xlsx"
    assert not run.file

    run, content = _export("contacts", {"scope": "all", "q": ""})
    assert _names(content, "contacts") == ["buyer Acme", "Stranger"]
    assert run.counts == {"rows": 2}

    run, content = _export("contacts", {"scope": "all", "q": " strang "})
    assert _names(content, "contacts") == [stranger.name]
    assert run.filters == {"scope": "all", "q": "strang"}

    run, _ = _export("contacts", {"scope": "nonsense"})
    assert run.filters == {"scope": "customers", "q": ""}
    assert ImportRun.objects.filter(kind=ImportRun.Kind.EXPORT).count() == 4


def test_export_honours_status_and_q():
    VendorFactory(name="Alpha Cars", email="alpha@acme.test")
    VendorFactory(name="Beta Limo", email="beta@acme.test", status=Vendor.Status.INACTIVE)
    VendorFactory(name="gamma Coaches", email="gamma@acme.test")

    run, content = _export("vendors", {"status": "active", "q": ""})
    assert _names(content, "vendors") == ["Alpha Cars", "gamma Coaches"]
    assert run.screen == ImportRun.Screen.VENDORS
    assert run.counts == {"rows": 2}
    assert run.filters == {"status": "active", "q": ""}
    assert run.filename == f"vendors-{timezone.localdate():%Y-%m-%d}.xlsx"

    run, content = _export("vendors", {"status": "inactive", "q": ""})
    assert _names(content, "vendors") == ["Beta Limo"]

    run, content = _export("vendors", {"status": "all", "q": "alpha"})
    assert _names(content, "vendors") == ["Alpha Cars"]

    run, content = _export("vendors", {"status": "bogus", "q": ""})
    assert _names(content, "vendors") == ["Alpha Cars", "gamma Coaches"]
    assert run.filters == {"status": "active", "q": ""}


def _contacts_with_everything(n: int) -> None:
    for i in range(n):
        contact = ContactFactory(
            email=f"c{i}@acme.test",
            phone=f"+1202555{1000 + i}",
            company=CompanyFactory(),
            primary_address=AddressFactory(line1=f"{i} Main St"),
        )
        ContactPhone.objects.create(contact=contact, number=f"+1617555{1000 + i}")


def _vendors_with_everything(n: int) -> None:
    types = [VehicleTypeFactory(name=name) for name in ("Sedan", "SUV")]
    for i in range(n):
        vendor = VendorFactory(
            email=f"v{i}@acme.test", address=AddressFactory(line1=f"{i} Main St")
        )
        vendor.vehicle_types.set(types)


@pytest.mark.parametrize("n", [5, 20])
@pytest.mark.parametrize(
    ("screen", "make", "filters", "queries"),
    [
        # Contacts with company + address, their phones, the run's insert.
        ("contacts", _contacts_with_everything, {"scope": "all"}, 3),
        # Vendors with address, their vehicle types, the Accepted values types, the insert.
        ("vendors", _vendors_with_everything, {"status": "all"}, 4),
    ],
)
def test_export_query_count_is_fixed(
    settings, django_assert_num_queries, screen, make, filters, queries, n
):
    """Fixed for any N, straddling the library's export chunk size (it pages a prefetched
    queryset by chunk: a COUNT, a page and a prefetch per chunk)."""
    settings.IMPORT_EXPORT_CHUNK_SIZE = 2
    make(n)
    user = UserFactory()
    with django_assert_num_queries(queries):
        run, content = services.export_rows(get_screen(screen), filters, user)
    assert run.counts == {"rows": n}
    assert len(_names(content, screen)) == n


# --- round trip (spec §3.3, §5.3, §6.1, §11) ------------------------------------------------


def _contact_fixtures() -> None:
    ContactFactory(name="Legacy Raw", email="", phone="202-555-0143")
    intl = ContactFactory(
        name="Intl Guest",
        email="intl@acme.test",
        phone="+442079460958",
        company=CompanyFactory(name="Acme Events"),
        channel=Channel.WEDDING_PRO,
        notes="n" * 33_000,
        primary_address=AddressFactory(
            line1="1 Main St", line2="Suite 2", city="Boston", state="MA", postal="02134"
        ),
    )
    contact_services.add_phone(intl, "(617) 555-0102", label="work")
    ContactFactory(name="=SUM(A1)", email="formula@acme.test", phone="", notes="-1 +2 @x")
    # Stored text the workbook can't carry exactly: line endings, outer whitespace, and a
    # control character Excel refuses. None of it is a change.
    ContactFactory(name="Sam ", email="sam@acme.test", phone="", notes="line one\r\nline two")
    ContactFactory(name="Tab\x0bbed", email="ctrl@acme.test", phone="", notes="a\x0bb")


def _vendor_fixtures() -> None:
    sedan = VehicleTypeFactory(name="Sedan")
    retired = VehicleTypeFactory(name="Retired Coach", active=False)
    VendorFactory(name="Legacy Limo", email="", phone="202-555-0143")
    VendorFactory(
        name="Intl Cars",
        email="Ops@acme.test",
        phone="+442079460958",
        website="https://intl.acme.test",
        usdot_number="0123",
        notes="n" * 33_000,
        address=AddressFactory(line1="1 Main St", city="Boston", state="MA", postal="02134"),
    ).vehicle_types.set([sedan, retired])
    VendorFactory(name="Odd Number", email="odd@acme.test", phone="555-0100 ext 4")
    VendorFactory(
        name="Parked",
        email="parked@acme.test",
        status=Vendor.Status.INACTIVE,
        notes="line one\r\nline two\x0b ",
    )


@pytest.mark.parametrize(
    ("screen", "fixtures", "filters", "total"),
    [
        ("contacts", _contact_fixtures, {"scope": "all"}, 5),
        ("vendors", _vendor_fixtures, {"status": "all"}, 4),
    ],
)
def test_round_trip_is_all_unchanged(screen, fixtures, filters, total):
    fixtures()
    model = Contact if screen == "contacts" else Vendor
    before = model.objects.count()
    stored = list(model.objects.order_by("pk").values())
    _, content = _export(screen, filters)
    run = _upload(content, screen=screen)
    services.preview(run)
    run.refresh_from_db()
    assert run.status == ImportRun.Status.PREVIEWED, run.error
    assert [r["outcome"] for r in run.rows] == ["unchanged"] * total, run.rows
    assert run.preview_counts["errors"] == 0
    assert run.preview_counts["examples"] == 0
    services.apply(run.pk, user=run.user)
    run.refresh_from_db()
    assert run.counts["unchanged"] == total
    assert model.objects.count() == before
    assert list(model.objects.order_by("pk").values()) == stored  # nothing rewritten


def test_export_writes_legacy_phones_as_stored():
    """A phone that isn't E.164 is written as stored, so it matches on re-import (only an
    E.164 number is formatted for reading)."""
    VendorFactory(name="Legacy Limo", email="", phone="202-555-0143")
    contact = ContactFactory(name="Legacy Raw", email="raw@acme.test", phone="202-555-0143")
    ContactPhone.objects.create(contact=contact, number="617.555.0102", label="home")
    contact_services.add_phone(contact, "(617) 555-0199", label="work")

    _, content = _export("vendors", {"status": "all"})
    parsed = read_rows(io.BytesIO(content), sheet="Vendors", columns=VENDOR_COLUMNS)
    assert parsed.dataset.dict[0]["Phone"] == "202-555-0143"

    _, content = _export("contacts", {"scope": "all"})
    parsed = read_rows(io.BytesIO(content), sheet="Contacts", columns=CONTACT_COLUMNS)
    row = parsed.dataset.dict[0]
    assert row["Phone"] == "202-555-0143"
    assert row["Other phones"] == "617.555.0102 (Home), (617) 555-0199 (Work)"


# --- error download (spec §4.5 step 4) ------------------------------------------------------


def test_error_download_has_reason_last_and_original_values():
    run = _previewed(
        ["Name", "Email", "Channel", "Notes"],
        [
            ["Sam", "sam@acme.test", "Website", ""],
            ["Dee", "DEE@acme.test", "Carrier pigeon", "Keep me"],
            ["Nobody", "", "", "No key"],
        ],
    )
    ImportRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(days=4))
    assert services.purge_files() == 1
    run.refresh_from_db()
    assert not run.file

    wb = load_workbook(io.BytesIO(services.error_rows(run)))
    ws = wb["Contacts"]
    values = [list(row) for row in ws.iter_rows(values_only=True)]
    assert values[0] == ["Name*", *CONTACT_HEADERS[1:], "Reason"]
    by_name = [dict(zip([*CONTACT_HEADERS, "Reason"], row, strict=True)) for row in values[1:]]
    assert [r["Name"] for r in by_name] == ["Dee", "Nobody"]
    assert by_name[0]["Email"] == "DEE@acme.test"
    assert by_name[0]["Channel"] == "Carrier pigeon"
    assert by_name[0]["Notes"] == "Keep me"
    assert by_name[0]["Phone"] is None
    assert by_name[0]["Reason"].startswith("Channel 'Carrier pigeon' isn't one of")
    assert by_name[1]["Reason"] == "Needs an email or a phone to match or create a contact."
    assert ACCEPTED_SHEET in wb.sheetnames


# --- template (spec §3.6, §4.4) -------------------------------------------------------------


def _accepted(content: bytes) -> dict[str, list[str]]:
    ws = load_workbook(io.BytesIO(content))[ACCEPTED_SHEET]
    columns: dict[str, list[str]] = {}
    for col in ws.iter_cols(values_only=True):
        if col[0]:
            columns[col[0]] = [v for v in col[1:] if v is not None]
    return columns


def test_template_has_examples_and_accepted_values():
    VehicleType.objects.update(active=False)  # the seeded types
    VehicleTypeFactory(name="Sprinter", sort_order=2)
    VehicleTypeFactory(name="Sedan", sort_order=1)
    VehicleTypeFactory(name="Coach", sort_order=1)
    VehicleTypeFactory(name="Retired Bus", sort_order=0, active=False)

    contacts = services.template_bytes(get_screen("contacts"))
    parsed = read_rows(io.BytesIO(contacts), sheet="Contacts", columns=CONTACT_COLUMNS)
    assert parsed.examples == 2
    assert len(parsed.dataset) == 0
    assert _accepted(contacts) == {
        "Channel": ["Website", "Wedding Pro", "Phone", "API", "GNet"],
        "Phone label": ["Mobile", "Work", "Home", "Other"],
        "Rules": RULES,
    }
    ws = load_workbook(io.BytesIO(contacts))["Contacts"]
    assert [c.value for c in ws[2]] == [
        "Jordan Example",
        "jordan@example.com",
        "(202) 555-0143",
        "(202) 555-0199 (Work)",
        "Example Events LLC",
        "Website",
        "1600 Pennsylvania Ave NW",
        None,
        "Washington",
        "DC",
        "20500",
        "Prefers the Escalade",
    ]
    assert [c.value for c in ws[3]][:2] == ["Sam Example", "sam@example.com"]

    vendors = services.template_bytes(get_screen("vendors"))
    parsed = read_rows(io.BytesIO(vendors), sheet="Vendors", columns=VENDOR_COLUMNS)
    assert parsed.examples == 1
    assert len(parsed.dataset) == 0
    assert _accepted(vendors) == {
        "Status": ["Active", "Inactive"],
        "Vehicle types": ["Coach", "Sedan", "Sprinter"],
        "Rules": RULES,
    }
    row = dict(
        zip(
            [c.name for c in VENDOR_COLUMNS],
            [c.value for c in load_workbook(io.BytesIO(vendors))["Vendors"][2]],
            strict=True,
        )
    )
    assert row["Email"] == "dispatch@example.com"
    assert row["Vehicle types"] == "Coach, Sedan"
    assert row["Status"] == "Active"
    assert not ImportRun.objects.exists()


# --- screens and selectors (spec §4.2, §10) -------------------------------------------------


def test_unknown_screen_is_404():
    with pytest.raises(Http404):
        get_screen("companies")
    assert set(SCREENS) == {"contacts", "vendors"}


def test_screen_queryset_is_ordered_by_name_then_pk():
    b = ContactFactory(name="bea", email="b1@acme.test")
    a = ContactFactory(name="Al", email="a@acme.test")
    b2 = ContactFactory(name="Bea", email="b2@acme.test")
    qs = get_screen("contacts").queryset({"scope": "all", "q": ""})
    assert list(qs) == [a, b, b2]
    assert qs.query.order_by == (Lower("name"), "pk")


def test_list_views_use_the_selectors(client):
    client.force_login(UserFactory())
    match = ContactFactory(name="Acme One", email="one@acme.test")
    LeadFactory(contact=match)
    ContactFactory(name="Acme Two", email="two@acme.test")
    ContactFactory(name="Other", email="three@other.test")
    resp = client.get(reverse("contact_list"), {"scope": "all", "q": "acme"})
    shown = {c.pk for c in resp.context["contacts"]}
    assert shown == set(contact_selectors.directory("all", "acme").values_list("pk", flat=True))
    assert len(shown) == 2
    resp = client.get(reverse("contact_list"), {"q": "acme"})
    assert {c.pk for c in resp.context["contacts"]} == {match.pk}

    VendorFactory(name="Acme Cars", email="cars@acme.test")
    VendorFactory(name="Acme Old", email="old@acme.test", status=Vendor.Status.INACTIVE)
    VendorFactory(name="Other", email="v@other.test")
    resp = client.get(reverse("vendor_list"), {"status": "all", "q": "acme"})
    shown = {v.pk for v in [*resp.context["attention"], *resp.context["roster"]]}
    assert shown == set(vendor_selectors.directory("all", "acme").values_list("pk", flat=True))
    assert len(shown) == 2
