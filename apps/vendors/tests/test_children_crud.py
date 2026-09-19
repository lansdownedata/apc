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


def test_add_insurance_rejects_expiry_before_effective(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    today = timezone.localdate()
    resp = client.post(
        reverse("insurance_create", args=[vendor.pk]),
        {
            "insurer": "Acme Mutual",
            "policy_number": "P-9",
            "coverage_amount": "1000000",
            "effective_date": str(today),
            "expiry_date": str(today - timezone.timedelta(days=1)),
        },
    )
    assert resp.status_code == 200
    assert VendorInsurance.objects.count() == 0


def test_add_document_sets_uploaded_by(client, django_user_model):
    user = _login(client, django_user_model)
    vendor = VendorFactory()
    f = SimpleUploadedFile("w9.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(reverse("document_create", args=[vendor.pk]), {"label": "W-9", "file": f})
    assert resp.status_code == 302
    doc = VendorDocument.objects.get(vendor=vendor)
    assert doc.uploaded_by == user


@pytest.mark.parametrize(
    ("url_name", "field"),
    [("insurance_create", "certificate"), ("document_create", "file")],
)
def test_file_fields_use_the_styled_uploader_not_native_chrome(
    client, django_user_model, url_name, field
):
    """The browser's "Choose File / No file chosen" control must never show: the real
    input stays in the DOM (sr-only, still submittable) under the shared dropzone."""
    _login(client, django_user_model)
    html = client.get(reverse(url_name, args=[VendorFactory().pk])).content.decode()
    assert 'x-data="imageUpload(' in html
    assert html.count('type="file"') == 1
    assert f'name="{field}"' in html
    file_input = html[html.index('<input type="file"') :].split(">", 1)[0]
    assert "sr-only" in file_input


def test_insurance_edit_links_certificate_on_file(client, django_user_model, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    _login(client, django_user_model)
    today = timezone.localdate()
    policy = VendorInsurance.objects.create(
        vendor=VendorFactory(),
        insurer="Acme Mutual",
        effective_date=today,
        expiry_date=today.replace(year=today.year + 1),
        certificate=SimpleUploadedFile("coi.pdf", b"%PDF-1.4 test"),
    )
    html = client.get(reverse("insurance_edit", args=[policy.pk])).content.decode()
    assert f'href="{policy.certificate.url}"' in html
