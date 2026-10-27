"""Affiliate document types (Settings-managed) replace the hard-coded document kinds."""

import importlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.vendors import compliance
from apps.vendors.factories import (
    AffiliateDocumentTypeFactory,
    VendorDocumentFactory,
    VendorFactory,
)
from apps.vendors.models import AffiliateDocumentType, VendorDocument

pytestmark = pytest.mark.django_db

seed = importlib.import_module("apps.vendors.migrations.0010_seed_document_types")


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())


def test_kind_mapping():
    assert seed.type_name_for("w9") == "W-9"
    assert seed.type_name_for("operating_authority") == "DOT operating authority"
    assert seed.type_name_for("affiliate_agreement") == "Affiliate agreement"
    assert seed.type_name_for("other") == "Other"
    assert seed.type_name_for("weird") == "Other"


def test_seeded_types():
    got = {
        t.name: (t.required, t.sort_order, t.active) for t in AffiliateDocumentType.objects.all()
    }
    assert got == {
        "DOT operating authority": (True, 10, True),
        "W-9": (True, 20, True),
        "Affiliate agreement": (True, 30, True),
        "Other": (False, 100, True),
    }
    assert [t.name for t in AffiliateDocumentType.objects.all()][0] == "DOT operating authority"


def test_other_is_get_or_create():
    other = AffiliateDocumentType.objects.other()
    assert other.name == "Other" and other.required is False
    assert AffiliateDocumentType.objects.other().pk == other.pk
    AffiliateDocumentType.objects.filter(name="Other").delete()
    assert AffiliateDocumentType.objects.other().required is False


def test_compliance_flags_active_required_only():
    AffiliateDocumentType.objects.filter(name="Affiliate agreement").update(active=False)
    AffiliateDocumentTypeFactory(name="Insurance rider", required=False, sort_order=50)
    v = VendorFactory(name="Bare")

    [row] = compliance.report()

    assert row.vendor == v
    assert row.missing_labels == ["DOT operating authority", "W-9"]


def test_compliance_query_count_fixed():
    VendorFactory(name="One")
    with CaptureQueriesContext(connection) as one:
        compliance.report()
    for i in range(4):
        VendorFactory(name=f"More {i}")
    with CaptureQueriesContext(connection) as five:
        compliance.report()
    assert len(five) == len(one)


def test_upload_defaults_to_other(client, staff):
    vendor = VendorFactory()
    f = SimpleUploadedFile("a.pdf", b"%PDF-1.4 test", content_type="application/pdf")

    resp = client.post(reverse("document_create", args=[vendor.pk]), {"label": "Misc", "file": f})

    assert resp.status_code == 302
    assert VendorDocument.objects.get(vendor=vendor).document_type.name == "Other"


def test_upload_lists_only_active_types(client, staff):
    AffiliateDocumentTypeFactory(name="Retired form", active=False)
    vendor = VendorFactory()

    resp = client.get(reverse("document_create", args=[vendor.pk]))
    body = resp.content.decode()

    assert 'name="document_type"' in body
    assert "data-tom" in body
    assert "W-9" in body
    assert "Retired form" not in body


def test_detail_shows_type_name(client, staff):
    vendor = VendorFactory()
    w9 = AffiliateDocumentType.objects.get(name="W-9")
    VendorDocumentFactory(vendor=vendor, label="Scan one", document_type=w9)
    VendorDocumentFactory(vendor=vendor, label="Scan two")

    body = client.get(reverse("vendor_detail", args=[vendor.pk])).content.decode()

    assert 'ml-2">W-9</span>' in body
    assert 'ml-2">Other</span>' not in body
