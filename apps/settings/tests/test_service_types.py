"""Service types — owner-admin only, deactivate instead of delete when trips use one."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import ServiceTypeFactory
from apps.leads.models import ServiceType
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def test_list_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("service_type_list")).status_code == 403


def test_owner_sees_seeded_and_custom_types(client):
    _owner(client)
    ServiceTypeFactory(name="Wine Tour")
    body = client.get(reverse("service_type_list")).content
    assert b"Airport Transfer" in body  # from the starter catalog
    assert b"Wine Tour" in body


def test_create(client):
    _owner(client)
    resp = client.post(
        reverse("service_type_create"),
        {"name": "Wine Tour", "sort_order": 5, "active": "on"},
    )
    assert resp.status_code == 302
    assert ServiceType.objects.get(name="Wine Tour").sort_order == 5


def test_create_rejects_a_duplicate_name_case_insensitively(client):
    _owner(client)
    ServiceTypeFactory(name="Wine Tour")
    resp = client.post(
        reverse("service_type_create"), {"name": "wine tour", "sort_order": 0, "active": "on"}
    )
    assert resp.status_code == 200, "re-renders the form rather than 500-ing on the constraint"
    assert ServiceType.objects.filter(name__iexact="wine tour").count() == 1


def test_edit(client):
    _owner(client)
    st = ServiceTypeFactory(name="Wine Tour")
    client.post(
        reverse("service_type_edit", args=[st.pk]),
        {"name": "Winery Tour", "sort_order": 0, "active": "on"},
    )
    st.refresh_from_db()
    assert st.name == "Winery Tour"


def test_delete_deactivates_when_a_trip_uses_it(client):
    """SET_NULL would blank the service on historical quotes — deactivate instead."""
    _owner(client)
    st = ServiceTypeFactory(name="Booked Service")
    ReservationFactory(service_type=st)
    client.post(reverse("service_type_delete", args=[st.pk]))
    st.refresh_from_db()
    assert st.active is False


def test_delete_removes_when_unused(client):
    _owner(client)
    st = ServiceTypeFactory(name="Never Used")
    client.post(reverse("service_type_delete", args=[st.pk]))
    assert not ServiceType.objects.filter(pk=st.pk).exists()


def test_settings_index_counts_and_links_them(client):
    _owner(client)
    resp = client.get(reverse("settings_index"))
    assert resp.context["service_type_count"] == ServiceType.objects.filter(active=True).count()
    assert b"Service types" in resp.content
