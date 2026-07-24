import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.vendors.factories import VendorFactory
from apps.vendors.models import VendorDocument, VendorDriver, VendorInsurance

pytestmark = pytest.mark.django_db


def _login(client, django_user_model):
    user = django_user_model.objects.create_user(username="agent", password="x")
    client.force_login(user)
    return user


def test_add_driver(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    resp = client.post(
        reverse("driver_create", args=[vendor.pk]),
        {"name": "Sam Root", "phone": "617-555-0111", "active": "on"},
    )
    assert resp.status_code == 302
    assert VendorDriver.objects.filter(vendor=vendor, name="Sam Root").exists()


def test_add_insurance_with_certificate(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    today = timezone.localdate()
    coi = SimpleUploadedFile("coi.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(
        reverse("insurance_create", args=[vendor.pk]),
        {
            "insurer": "Acme Mutual",
            "policy_number": "P-9",
            "coverage_amount": "1000000",
            "effective_date": str(today),
            "expiry_date": str(today.replace(year=today.year + 1)),
            "certificate": coi,
        },
    )
    assert resp.status_code == 302
    policy = VendorInsurance.objects.get(vendor=vendor)
    assert policy.certificate.name.endswith(".pdf")


def test_add_document_sets_uploaded_by(client, django_user_model):
    user = _login(client, django_user_model)
    vendor = VendorFactory()
    f = SimpleUploadedFile("w9.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(reverse("document_create", args=[vendor.pk]), {"label": "W-9", "file": f})
    assert resp.status_code == 302
    doc = VendorDocument.objects.get(vendor=vendor)
    assert doc.uploaded_by == user
