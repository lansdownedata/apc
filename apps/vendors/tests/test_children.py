import pytest
from django.contrib.auth import get_user_model

from apps.vendors.factories import VendorFactory
from apps.vendors.models import VendorDocument, VendorDriver

pytestmark = pytest.mark.django_db


def test_driver_belongs_to_vendor_and_defaults_active():
    vendor = VendorFactory()
    driver = VendorDriver.objects.create(vendor=vendor, name="Sam Root")
    assert driver.active is True
    assert list(vendor.drivers.all()) == [driver]


def test_document_records_uploader_and_upload_time():
    user = get_user_model().objects.create_user(username="uploader-a", password="x")
    vendor = VendorFactory()
    doc = VendorDocument.objects.create(vendor=vendor, label="W-9", uploaded_by=user)
    assert doc.uploaded_by == user
    assert doc.created_at is not None  # created_at is the uploaded-on timestamp
    assert list(vendor.documents.all()) == [doc]


def test_document_uploader_set_null_on_user_delete():
    user = get_user_model().objects.create_user(username="uploader-c", password="x")
    doc = VendorDocument.objects.create(vendor=VendorFactory(), label="COI", uploaded_by=user)
    user.delete()
    doc.refresh_from_db()
    assert doc.uploaded_by is None
