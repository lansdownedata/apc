from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.fleet.factories import DriverFactory, RenewalFactory, VehicleFactory
from apps.fleet.models import Driver, Vehicle

pytestmark = pytest.mark.django_db


def _in(days):
    return timezone.localdate() + timedelta(days=days)


def test_create_driver_redirects_to_detail_and_numbers_it(logged_in_client):
    resp = logged_in_client.post(
        reverse("fleet:driver_create"),
        {"name": "Marcus Bell", "phone": "(571) 555-0177", "status": "active", "notes": ""},
    )
    d = Driver.objects.get(name="Marcus Bell")
    assert resp.status_code == 302
    assert resp.url == reverse("fleet:driver_detail", args=[d.pk])
    assert d.driver_number == 1000
    assert d.phone == "+15715550177"


def test_edit_ignores_a_posted_driver_number(logged_in_client):
    d = DriverFactory(name="Marcus Bell")
    logged_in_client.post(
        reverse("fleet:driver_edit", args=[d.pk]),
        {"name": "Marcus A. Bell", "status": "active", "notes": "", "driver_number": "4242"},
    )
    d.refresh_from_db()
    assert d.name == "Marcus A. Bell"
    assert d.driver_number == 1000


def test_driver_detail_shows_current_and_history(logged_in_client):
    d = DriverFactory(name="Marcus Bell")
    old = RenewalFactory(driver=d, reference="OLD-1", expires_on=_in(-40))
    new = RenewalFactory(
        driver=d, renewal_type=old.renewal_type, reference="NEW-2", expires_on=_in(300)
    )
    resp = logged_in_client.get(reverse("fleet:driver_detail", args=[d.pk]))
    assert resp.status_code == 200
    assert [r.pk for r in resp.context["current"]] == [new.pk]
    assert [r.pk for r in resp.context["history"]] == [old.pk]
    assert b"NEW-2" in resp.content and b"OLD-1" in resp.content
    assert b"smartAddress(" in resp.content


def test_driver_detail_is_query_flat(logged_in_client, django_assert_max_num_queries):
    d = DriverFactory()
    for _ in range(5):
        RenewalFactory(driver=d)
    with django_assert_max_num_queries(10):
        logged_in_client.get(reverse("fleet:driver_detail", args=[d.pk]))


def test_driver_address_update_lazily_creates(logged_in_client):
    d = DriverFactory()
    resp = logged_in_client.post(
        reverse("fleet:driver_address_update", args=[d.pk]),
        {"line1": "12 Elm St", "city": "Arlington", "state": "VA", "postal": "22201"},
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    d.refresh_from_db()
    assert d.home_address.line1 == "12 Elm St"


def test_driver_address_update_is_post_only(logged_in_client):
    d = DriverFactory()
    assert (
        logged_in_client.get(reverse("fleet:driver_address_update", args=[d.pk])).status_code == 405
    )


def test_create_vehicle(logged_in_client):
    from apps.leads.factories import VehicleTypeFactory

    vt = VehicleTypeFactory(name="Luxury SUV")
    resp = logged_in_client.post(
        reverse("fleet:vehicle_create"),
        {
            "name": "Unit 1",
            "vehicle_type": vt.pk,
            "year": 2023,
            "make": "Chevrolet",
            "model_name": "Suburban",
            "color": "Black",
            "license_plate": "APC-0001",
            "vin": "",
            "status": "active",
            "notes": "",
        },
    )
    v = Vehicle.objects.get(name="Unit 1")
    assert resp.status_code == 302 and resp.url == reverse("fleet:vehicle_detail", args=[v.pk])


def test_vehicle_detail_renders(logged_in_client):
    v = VehicleFactory(name="Unit 1")
    resp = logged_in_client.get(reverse("fleet:vehicle_detail", args=[v.pk]))
    assert resp.status_code == 200 and b"Unit 1" in resp.content


def test_existing_address_endpoints_still_work_after_extraction(logged_in_client):
    """The vendor + contact auto-save views now share apply_posted_address."""
    from apps.contacts.factories import ContactFactory
    from apps.vendors.factories import VendorFactory

    vendor = VendorFactory()
    resp = logged_in_client.post(
        reverse("vendor_address_update", args=[vendor.pk]), {"city": "Reston", "latitude": ""}
    )
    vendor.refresh_from_db()
    assert resp.json()["ok"] is True and vendor.address.city == "Reston"
    assert vendor.address.latitude is None

    contact = ContactFactory()
    resp = logged_in_client.post(
        reverse("contact_address_update", args=[contact.pk, "primary"]), {"place_id": "abc"}
    )
    contact.refresh_from_db()
    assert resp.json()["ok"] is True and contact.primary_address.locationiq_place_id == "abc"
