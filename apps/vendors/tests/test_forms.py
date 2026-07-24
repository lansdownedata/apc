import pytest
from django.urls import reverse

from apps.leads.models import VehicleType
from apps.vendors.factories import VendorFactory
from apps.vendors.models import Vendor

pytestmark = pytest.mark.django_db


def _login(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="agent", password="x"))


def test_create_vendor_normalizes_phone_and_saves_types(client, django_user_model):
    _login(client, django_user_model)
    suv = VehicleType.objects.create(name="SUV")
    resp = client.post(
        reverse("vendor_create"),
        {
            "name": "Elite Sedans",
            "phone": "(617) 555-0200",
            "status": "active",
            "vehicle_types": [suv.pk],
        },
    )
    assert resp.status_code == 302
    vendor = Vendor.objects.get(name="Elite Sedans")
    assert vendor.phone == "+16175550200"
    assert list(vendor.vehicle_types.all()) == [suv]


def test_edit_vendor_updates_fields(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory(name="Old Name")
    resp = client.post(
        reverse("vendor_edit", args=[vendor.pk]),
        {"name": "New Name", "status": "inactive"},
    )
    assert resp.status_code == 302
    vendor.refresh_from_db()
    assert vendor.name == "New Name"
    assert vendor.status == Vendor.Status.INACTIVE
