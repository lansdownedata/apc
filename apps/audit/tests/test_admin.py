"""The read-only admin over the trail (spec §8)."""

import pytest
from django.contrib import admin
from django.urls import reverse

from apps.audit.models import AuditEntry
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser_client(client, django_user_model):
    root = django_user_model.objects.create_superuser(username="root", password="pw", email="")
    client.force_login(root)
    return client


def test_changelist_and_entry_render(superuser_client):
    ContactFactory(name="Ada Ruiz")
    entry = AuditEntry.objects.latest("pk")

    listing = superuser_client.get(reverse("admin:audit_auditentry_changelist"))
    detail = superuser_client.get(reverse("admin:audit_auditentry_change", args=[entry.pk]))

    assert listing.status_code == 200
    assert detail.status_code == 200
    assert b"Ada Ruiz" in listing.content


def test_source_and_actor_email_are_listed_and_filterable(rf):
    model_admin = admin.site._registry[AuditEntry]

    assert {"source", "actor_email"} <= set(model_admin.list_display)
    assert {"source", "actor_email"} <= set(model_admin.list_filter)


def test_entries_cannot_be_added_or_changed(superuser_client, rf):
    model_admin = admin.site._registry[AuditEntry]
    request = rf.get("/")

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    response = superuser_client.post(reverse("admin:audit_auditentry_add"), {})
    assert response.status_code == 403


def test_the_registered_admin_is_ours():
    from apps.audit.admin import AuditEntryAdmin

    assert isinstance(admin.site._registry[AuditEntry], AuditEntryAdmin)
