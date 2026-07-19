import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead():
    return LeadFactory(contact=ContactFactory(phone="(202) 555-0100"))


@pytest.fixture
def agent_client(client):
    client.force_login(UserFactory())
    return client


def test_update_normalizes_phone(agent_client, lead):
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": "305-555-0199"})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+13055550199"


def test_update_other_fields_without_phone_still_works(agent_client, lead):
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"company": "Beltway"})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.company == "Beltway"
    assert lead.contact.phone == "+12025550100"


def test_update_rejects_unparseable_phone(agent_client, lead):
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": "nonsense"})
    assert resp.status_code == 400
    assert "phone" in resp.json()["error"].lower()
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+12025550100"


def test_update_clearing_phone_removes_primary(agent_client, lead):
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == ""
