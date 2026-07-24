import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.models import Contact

pytestmark = pytest.mark.django_db


def test_contact_create_makes_contact_and_company(client):
    client.force_login(UserFactory())
    resp = client.post(
        reverse("contact_create"),
        {"name": "Priya", "company": "Anand Family Office", "channel": "phone"},
    )
    assert resp.status_code == 302
    c = Contact.objects.get(name="Priya")
    assert c.company.name == "Anand Family Office"
    assert resp.url == reverse("contact_detail", args=[c.pk])


def test_contact_create_dedupes_by_email(client):
    from apps.contacts.factories import ContactFactory

    existing = ContactFactory(email="priya@example.com")
    client.force_login(UserFactory())
    resp = client.post(
        reverse("contact_create"),
        {"name": "Priya 2", "email": "priya@example.com", "channel": "website"},
    )
    assert Contact.objects.count() == 1
    assert resp.url == reverse("contact_detail", args=[existing.pk])
