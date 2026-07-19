"""Vehicle Types CRUD — owner-admin only, guarded delete."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType

pytestmark = pytest.mark.django_db

# A 1x1 transparent GIF — the smallest valid image Pillow will accept.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def test_list_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("vehicle_type_list")).status_code == 403


def test_owner_admin_sees_the_list(client):
    client.force_login(UserFactory(role="owner_admin"))
    VehicleTypeFactory(name="Luxury SUV")
    response = client.get(reverse("vehicle_type_list"))
    assert response.status_code == 200
    assert b"Luxury SUV" in response.content


def test_create_with_an_image(client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client.force_login(UserFactory(role="owner_admin"))
    response = client.post(
        reverse("vehicle_type_create"),
        {
            "name": "28-Passenger Mini Bus",
            "capacity": 28,
            "description": "Perfect for wedding parties.",
            "sort_order": 3,
            "active": "on",
            "image": SimpleUploadedFile("bus.gif", TINY_GIF, content_type="image/gif"),
        },
    )
    assert response.status_code == 302
    vt = VehicleType.objects.get(name="28-Passenger Mini Bus")
    assert vt.capacity == 28
    assert "vehicle-types/" in vt.image.name


def test_create_without_an_image_is_allowed(client):
    client.force_login(UserFactory(role="owner_admin"))
    client.post(
        reverse("vehicle_type_create"),
        {"name": "Trolley", "capacity": 30, "description": "", "sort_order": 0, "active": "on"},
    )
    assert VehicleType.objects.filter(name="Trolley").exists()


def test_delete_deactivates_when_in_use(client):
    from apps.reservations.factories import ReservationFactory

    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleTypeFactory(name="In Use SUV")
    ReservationFactory(vehicle=vt)
    client.post(reverse("vehicle_type_delete", args=[vt.pk]))
    vt.refresh_from_db()
    assert vt.active is False, "a referenced type must survive so old quotes still render"


def test_delete_removes_when_unused(client):
    client.force_login(UserFactory(role="owner_admin"))
    vt = VehicleTypeFactory(name="Never Booked")
    client.post(reverse("vehicle_type_delete", args=[vt.pk]))
    assert not VehicleType.objects.filter(pk=vt.pk).exists()
