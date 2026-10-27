"""Affiliate document types — owner-admin only, deactivate instead of delete when referenced."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.vendors.factories import AffiliateDocumentTypeFactory, VendorDocumentFactory
from apps.vendors.models import AffiliateDocumentType

pytestmark = pytest.mark.django_db


def _owner(client):
    client.force_login(UserFactory(role="owner_admin"))


def _messages(resp) -> list[str]:
    return [str(m) for m in get_messages(resp.wsgi_request)]


def test_list_requires_owner_admin(client):
    client.force_login(UserFactory(role="agent"))
    assert client.get(reverse("document_type_list")).status_code == 403


def test_list_shows_fixed_coi_row_first(client):
    _owner(client)
    body = client.get(reverse("document_type_list")).content.decode()
    assert body.index("Certificate of insurance") < body.index("DOT operating authority")
    assert "tracked under insurance" in body.lower()


def test_create(client):
    _owner(client)
    resp = client.post(
        reverse("document_type_create"),
        {"name": "Safety plan", "required": "on", "sort_order": 30, "active": "on"},
    )
    assert resp.status_code == 302
    t = AffiliateDocumentType.objects.get(name="Safety plan")
    assert t.required and t.sort_order == 30 and t.active


def test_edit(client):
    _owner(client)
    t = AffiliateDocumentTypeFactory(name="Safty plan")
    client.post(
        reverse("document_type_edit", args=[t.pk]),
        {"name": "Safety plan", "sort_order": 4, "active": "on"},
    )
    t.refresh_from_db()
    assert t.name == "Safety plan" and t.required is False and t.sort_order == 4


def test_delete_deactivates_when_referenced(client):
    _owner(client)
    doc = VendorDocumentFactory(document_type=AffiliateDocumentTypeFactory(name="W-9 copy"))
    resp = client.post(reverse("document_type_delete", args=[doc.document_type.pk]))
    doc.document_type.refresh_from_db()
    assert doc.document_type.active is False
    assert "W-9 copy deactivated (it has documents)." in _messages(resp)


def test_delete_removes_when_unused(client):
    _owner(client)
    t = AffiliateDocumentTypeFactory()
    client.post(reverse("document_type_delete", args=[t.pk]))
    assert not AffiliateDocumentType.objects.filter(pk=t.pk).exists()


def test_other_name_is_ignored_on_edit(client):
    _owner(client)
    other = AffiliateDocumentType.objects.other()
    resp = client.post(
        reverse("document_type_edit", args=[other.pk]),
        {"name": "Misc", "required": "on", "sort_order": 7},
    )
    assert resp.status_code == 302
    other.refresh_from_db()
    assert other.name == "Other" and other.sort_order == 7 and other.required
    assert AffiliateDocumentType.objects.other().pk == other.pk


def test_other_stays_active_on_edit(client):
    _owner(client)
    other = AffiliateDocumentType.objects.other()
    client.post(reverse("document_type_edit", args=[other.pk]), {"sort_order": 100})
    other.refresh_from_db()
    assert other.active is True


def test_other_edit_page_disables_name(client):
    _owner(client)
    other = AffiliateDocumentType.objects.other()
    body = client.get(reverse("document_type_edit", args=[other.pk])).content.decode()
    assert body.count("disabled") >= 2
    assert reverse("document_type_delete", args=[other.pk]) not in body


def test_other_delete_is_refused(client):
    _owner(client)
    other = AffiliateDocumentType.objects.other()
    resp = client.post(reverse("document_type_delete", args=[other.pk]))
    assert resp.status_code == 302 and resp.url == reverse("document_type_list")
    assert AffiliateDocumentType.objects.filter(pk=other.pk, active=True).exists()
    assert "Other is the default for uploads and can't be removed." in _messages(resp)
