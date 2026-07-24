import pytest

from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db


def test_status_defaults_to_active():
    vendor = Vendor.objects.create(name="Elite Sedans")
    assert vendor.status == Vendor.Status.ACTIVE


def test_match_or_create_normalizes_phone_and_dedupes():
    first = Vendor.objects.match_or_create(name="Elite Sedans", phone="(617) 555-0200")
    assert first.phone == "+16175550200"
    again = Vendor.objects.match_or_create(name="Elite Sedans (dup)", phone="617-555-0200")
    assert again.pk == first.pk, "same phone must return the existing vendor"


def test_find_match_by_email_is_case_insensitive():
    vendor = Vendor.objects.create(name="Elite", email="ops@elite.com")
    assert Vendor.objects.find_match(email="OPS@ELITE.COM") == vendor


def test_str_is_name():
    assert str(Vendor(name="Elite Sedans")) == "Elite Sedans"
