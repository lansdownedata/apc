"""Contact detail page — skeleton (header) plus the phone-numbers block.

The rest of the record-pages design (LTV chips, order history, inline header edit,
contact_create) is out of scope here — see the plan's scope boundary.
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


def test_detail_renders_contact(agent_client):
    contact = ContactFactory(name="Sarah Reyes", company="Beltway Capital")
    resp = agent_client.get(reverse("contact_detail", args=[contact.pk]))
    assert resp.status_code == 200
    assert "Sarah Reyes" in resp.content.decode()
    assert "Beltway Capital" in resp.content.decode()


def test_detail_lists_all_numbers_primary_first(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    contact.add_phone("(305) 555-0199", label="work")
    body = agent_client.get(reverse("contact_detail", args=[contact.pk])).content.decode()
    assert body.index("+12025550100") < body.index("+13055550199")


def test_detail_requires_login(client):
    contact = ContactFactory()
    resp = client.get(reverse("contact_detail", args=[contact.pk]))
    assert resp.status_code == 302
    assert "/login" in resp["Location"]


def test_detail_404s_for_unknown_contact(agent_client):
    assert agent_client.get(reverse("contact_detail", args=[999999])).status_code == 404
