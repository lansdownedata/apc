import pytest
from django.urls import reverse

from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _login(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="agent", password="x"))


def test_address_update_lazily_creates_and_writes(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    assert vendor.address is None
    resp = client.post(
        reverse("vendor_address_update", args=[vendor.pk]),
        {
            "line1": "1600 Pennsylvania Ave NW",
            "city": "Washington",
            "state": "DC",
            "postal": "20500",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    vendor.refresh_from_db()
    assert vendor.address is not None
    assert vendor.address.line1 == "1600 Pennsylvania Ave NW"
    assert vendor.address.city == "Washington"


def test_address_update_is_post_only(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    assert client.get(reverse("vendor_address_update", args=[vendor.pk])).status_code == 405


def test_detail_renders_smart_address(client, django_user_model):
    _login(client, django_user_model)
    vendor = VendorFactory()
    resp = client.get(reverse("vendor_detail", args=[vendor.pk]))
    assert b"smartAddress(" in resp.content  # the reusable widget is embedded
