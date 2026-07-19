"""Views for adding/removing/promoting a contact's phone numbers.

Object-level authz matters here: every phone view scopes its lookup by
`contact=contact` so acting on another contact's phone 404s rather than succeeding.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent_client(client):
    client.force_login(UserFactory())
    return client


def test_add_phone(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    resp = agent_client.post(
        reverse("contact_phone_add", args=[contact.pk]),
        {"phone": "305-555-0199", "label": "work"},
    )
    assert resp.status_code in (200, 302)
    assert contact.phones.filter(e164="+13055550199", label="work").exists()


def test_add_invalid_phone_is_rejected(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    resp = agent_client.post(
        reverse("contact_phone_add", args=[contact.pk]), {"phone": "junk", "label": ""}
    )
    assert resp.status_code == 400
    assert contact.phones.count() == 1


def test_set_primary_demotes_previous(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    secondary = contact.add_phone("(305) 555-0199", label="work")
    agent_client.post(reverse("contact_phone_primary", args=[contact.pk, secondary.pk]))
    contact.refresh_from_db()
    assert contact.phone == "+13055550199"
    assert contact.phones.filter(is_primary=True).count() == 1


def test_delete_phone(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    secondary = contact.add_phone("(305) 555-0199")
    agent_client.post(reverse("contact_phone_delete", args=[contact.pk, secondary.pk]))
    assert contact.phones.count() == 1


def test_phone_views_require_login(client):
    contact = ContactFactory(phone="(202) 555-0100")
    resp = client.post(reverse("contact_phone_add", args=[contact.pk]), {"phone": "x"})
    assert resp.status_code == 302
    assert "/login" in resp["Location"]


def test_cannot_touch_another_contacts_phone(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")
    foreign = other.phones.first()
    resp = agent_client.post(reverse("contact_phone_delete", args=[contact.pk, foreign.pk]))
    assert resp.status_code == 404
    assert other.phones.count() == 1
