"""Renewal types — owner-admin only, deactivate instead of delete when referenced."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.fleet.factories import DriverFactory, RenewalFactory, RenewalTypeFactory, VehicleFactory
from apps.fleet.models import RenewalType
from apps.leads.factories import VehicleTypeFactory

pytestmark = pytest.mark.django_db


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def test_list_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("renewal_type_list")).status_code == 403


def test_owner_sees_seeded_and_custom_types(client):
    _owner(client)
    RenewalTypeFactory(name="Medical card")
    body = client.get(reverse("renewal_type_list")).content
    assert b"Driver&#x27;s license" in body or b"Driver's license" in body
    assert b"Medical card" in body


def test_create(client):
    _owner(client)
    resp = client.post(
        reverse("renewal_type_create"),
        {"name": "Medical card", "applies_to": "driver", "sort_order": 5, "active": "on"},
    )
    assert resp.status_code == 302
    t = RenewalType.objects.get(name="Medical card")
    assert t.applies_to == "driver" and t.sort_order == 5


def test_edit(client):
    _owner(client)
    t = RenewalTypeFactory(name="Med card")
    client.post(
        reverse("renewal_type_edit", args=[t.pk]),
        {"name": "Medical card", "applies_to": "driver", "sort_order": 0, "active": "on"},
    )
    t.refresh_from_db()
    assert t.name == "Medical card"


def test_delete_deactivates_when_referenced(client):
    _owner(client)
    row = RenewalFactory()
    client.post(reverse("renewal_type_delete", args=[row.renewal_type.pk]))
    row.renewal_type.refresh_from_db()
    assert row.renewal_type.active is False


def test_delete_removes_when_unreferenced(client):
    _owner(client)
    t = RenewalTypeFactory()
    client.post(reverse("renewal_type_delete", args=[t.pk]))
    assert not RenewalType.objects.filter(pk=t.pk).exists()


def test_settings_index_counts_types_and_attention(client):
    _owner(client)
    d = DriverFactory()
    RenewalFactory(driver=d, expires_on=timezone.localdate() + timedelta(days=3))
    VehicleFactory()  # nothing on file → not attention
    resp = client.get(reverse("settings_index"))
    assert resp.context["renewal_type_count"] == RenewalType.objects.filter(active=True).count()
    assert resp.context["fleet_attention_count"] == 1
    assert b"Renewal types" in resp.content


def test_vehicle_type_delete_deactivates_when_a_unit_uses_it(client):
    _owner(client)
    vt = VehicleTypeFactory(name="Fleet Class")
    VehicleFactory(vehicle_type=vt)
    client.post(reverse("vehicle_type_delete", args=[vt.pk]))
    vt.refresh_from_db()
    assert vt.active is False, "PROTECT would 500 on a hard delete — must deactivate"
