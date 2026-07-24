import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


def test_contact_detail_renders(client):
    c = ContactFactory(name="Priya")
    client.force_login(UserFactory())
    resp = client.get(reverse("contact_detail", args=[c.pk]))
    assert resp.status_code == 200
    assert b"Priya" in resp.content


def test_contact_update_writes_fields_and_resolves_company(client):
    c = ContactFactory(name="Priya", company=None)
    client.force_login(UserFactory())
    resp = client.post(
        reverse("contact_update", args=[c.pk]),
        {"name": "Priya A", "company": "Anand Family Office"},
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    c.refresh_from_db()
    assert c.name == "Priya A"
    assert c.company.name == "Anand Family Office"


def test_contact_update_rejects_invalid_phone(client):
    c = ContactFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[c.pk]), {"phone": "12345"})
    assert resp.status_code == 400
    assert "valid phone" in resp.json()["error"]


def test_contact_update_rejects_duplicate_email(client):
    ContactFactory(email="taken@example.com")
    c = ContactFactory(email="me@example.com")
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[c.pk]), {"email": "TAKEN@example.com"})
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()
