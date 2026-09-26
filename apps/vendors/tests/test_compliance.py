"""APC-73 — the affiliate compliance report: worst insurance status first."""

import csv
import io
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.vendors import compliance
from apps.vendors.factories import VendorDocumentFactory, VendorFactory, VendorInsuranceFactory
from apps.vendors.models import Vendor, VendorDocument

pytestmark = pytest.mark.django_db


def _vendor(name, *days, **kw):
    v = VendorFactory(name=name, **kw)
    for d in days:
        VendorInsuranceFactory(vendor=v, expiry_date=timezone.localdate() + timedelta(days=d))
    return v


def _names(rows):
    return [r.vendor.name for r in rows]


def test_ordering_is_worst_status_first_then_soonest_expiry():
    _vendor("Valid far", 200)
    _vendor("Expiring 25", 25)
    _vendor("Expiring 20", 20)
    _vendor("Urgent", 12)
    _vendor("Critical", 5)
    _vendor("Expired long ago", -30)
    _vendor("Expired yesterday", -1)
    _vendor("No policy")
    _vendor("Inactive expired", -3, status=Vendor.Status.INACTIVE)

    rows = compliance.report()

    assert _names(rows) == [
        "Expired long ago",
        "Expired yesterday",
        "No policy",
        "Critical",
        "Urgent",
        "Expiring 20",
        "Expiring 25",
        "Valid far",
    ]


def test_worst_policy_governs_the_status_and_expiry():
    v = _vendor("Two policies", 300, -2)

    [row] = compliance.report()

    assert row.vendor == v
    assert row.status == "expired"
    assert row.expiry == timezone.localdate() - timedelta(days=2)


def test_missing_documents_counts_expected_kinds_not_on_file():
    v = _vendor("Some docs", 200)
    VendorDocumentFactory(vendor=v, kind=VendorDocument.Kind.W9)
    VendorDocumentFactory(vendor=v, kind=VendorDocument.Kind.W9)
    VendorDocumentFactory(vendor=v, kind=VendorDocument.Kind.OTHER)
    full = _vendor("All docs", 200)
    for kind in compliance.EXPECTED_DOCUMENT_KINDS:
        VendorDocumentFactory(vendor=full, kind=kind)

    by_name = {r.vendor.name: r for r in compliance.report()}

    assert by_name["Some docs"].missing_documents == len(compliance.EXPECTED_DOCUMENT_KINDS) - 1
    assert by_name["All docs"].missing_documents == 0
    assert "W-9" not in by_name["Some docs"].missing_labels


def test_status_filter():
    _vendor("Expired", -1)
    _vendor("Valid", 200)

    assert _names(compliance.report(status="expired")) == ["Expired"]
    assert _names(compliance.report(status="bogus")) == ["Expired", "Valid"]


def test_expiring_within_window_filter():
    _vendor("In 10", 10)
    _vendor("In 45", 45)
    _vendor("Lapsed", -3)
    _vendor("None on file")

    assert _names(compliance.report(within=30)) == ["Lapsed", "In 10"]
    assert _names(compliance.report(within=60)) == ["Lapsed", "In 10", "In 45"]


def test_counts_per_status_and_the_out_of_compliance_number():
    _vendor("A", -1)
    _vendor("B", -9)
    _vendor("C")
    _vendor("D", 5)
    _vendor("E", 200)

    counts = compliance.status_counts(compliance.report())

    assert counts == {
        "expired": 2,
        "none": 1,
        "critical": 1,
        "urgent": 0,
        "expiring": 0,
        "valid": 1,
    }
    assert compliance.out_of_compliance_count() == 3


def test_query_count_is_flat_across_fifty_vendors():
    for i in range(5):
        v = _vendor(f"V{i}", i * 10 - 5)
        VendorDocumentFactory(vendor=v, kind=VendorDocument.Kind.W9)
    with CaptureQueriesContext(connection) as few:
        list(compliance.report())
    for i in range(45):
        v = _vendor(f"W{i}", i * 7 - 20)
        VendorDocumentFactory(vendor=v, kind=VendorDocument.Kind.OTHER)
    with CaptureQueriesContext(connection) as many:
        rows = compliance.report()

    assert len(rows) == 50
    assert len(many) == len(few) <= 3


# --- the page + CSV ---------------------------------------------------------------


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())


def test_the_page_lists_the_rows_with_counts_and_a_sticky_header(client, staff):
    _vendor("Lapsed Limo", -2)
    _vendor("Solid Sedans", 200)

    resp = client.get(reverse("vendor_compliance"))
    body = resp.content.decode()

    assert resp.status_code == 200
    assert body.index("Lapsed Limo") < body.index("Solid Sedans")
    assert "sticky" in body
    assert resp.context["counts"]["expired"] == 1
    assert "<select" not in body or "data-tom" in body.split("<select", 1)[1].split(">")[0]


def test_the_page_filters_from_the_query_string(client, staff):
    _vendor("Lapsed Limo", -2)
    _vendor("Solid Sedans", 200)

    resp = client.get(reverse("vendor_compliance"), {"status": "valid"})

    assert _names(resp.context["rows"]) == ["Solid Sedans"]


def test_the_csv_matches_the_screen(client, staff):
    _vendor("Lapsed Limo", -2)
    _vendor("Coming Up Coaches", 12)
    _vendor("Solid Sedans", 200)
    params = {"within": "30"}

    page = client.get(reverse("vendor_compliance"), params)
    export = client.get(reverse("vendor_compliance"), {**params, "format": "csv"})

    assert export["Content-Type"].startswith("text/csv")
    table = list(csv.reader(io.StringIO(export.content.decode())))
    assert table[0] == ["Affiliate", "Status", "Soonest expiry", "Missing documents"]
    assert [row[0] for row in table[1:]] == _names(page.context["rows"])
    assert table[1][1] == "Expired"
    assert table[1][2] == (timezone.localdate() - timedelta(days=2)).isoformat()


def test_the_upload_form_asks_for_the_document_kind(client, staff):
    v = _vendor("Docs Inc", 200)

    body = client.get(reverse("document_create", args=[v.pk])).content.decode()

    assert 'name="kind"' in body
    assert "data-tom" in body
