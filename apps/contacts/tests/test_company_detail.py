import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Company

pytestmark = pytest.mark.django_db


def test_company_detail_lists_its_contacts(client):
    company = Company.objects.create(name="BigCo")
    ContactFactory(name="Priya", company=company)
    ContactFactory(name="Outsider", company=None)
    client.force_login(UserFactory())
    resp = client.get(reverse("company_detail", args=[company.pk]))
    assert resp.status_code == 200
    assert b"Priya" in resp.content
    assert b"Outsider" not in resp.content


def test_company_update_sets_billing_contact(client):
    company = Company.objects.create(name="BigCo")
    ap = ContactFactory(name="AP")
    client.force_login(UserFactory())
    resp = client.post(
        reverse("company_update", args=[company.pk]), {"billing_contact": str(ap.pk)}
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    company.refresh_from_db()
    assert company.billing_contact == ap


def test_company_update_rejects_invalid_billing_contact(client):
    company = Company.objects.create(name="BigCo")
    client.force_login(UserFactory())
    # non-numeric
    resp = client.post(reverse("company_update", args=[company.pk]), {"billing_contact": "abc"})
    assert resp.status_code == 400
    assert "billing contact" in resp.json()["error"].lower()
    # numeric but nonexistent
    resp = client.post(reverse("company_update", args=[company.pk]), {"billing_contact": "99999"})
    assert resp.status_code == 400


def test_company_update_rejects_duplicate_name(client):
    Company.objects.create(name="Acme")
    other = Company.objects.create(name="BigCo")
    client.force_login(UserFactory())
    resp = client.post(reverse("company_update", args=[other.pk]), {"name": "ACME"})
    assert resp.status_code == 400
    assert "already exists" in resp.json()["error"].lower()
