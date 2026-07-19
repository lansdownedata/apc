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


def test_update_rejects_number_owned_by_another_contact(agent_client, lead):
    ContactFactory(phone="(305) 555-0199")
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": "305-555-0199"})
    assert resp.status_code == 400
    assert "already assigned to another contact" in resp.json()["error"]
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+12025550100"


def test_update_clearing_phone_with_zero_numbers_is_a_noop(agent_client):
    lead = LeadFactory(contact=ContactFactory(phone=""))
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == ""
    assert lead.contact.phones.count() == 0


def test_update_clearing_phone_with_exactly_one_number_deletes_it(agent_client, lead):
    assert lead.contact.phones.count() == 1
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phones.count() == 0
    assert lead.contact.phone == ""


def test_update_clearing_phone_with_multiple_numbers_is_refused(agent_client, lead):
    lead.contact.add_phone("(305) 555-0199", label="work")
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})
    assert resp.status_code == 400
    assert "other numbers" in resp.json()["error"].lower()
    lead.contact.refresh_from_db()
    assert lead.contact.phones.count() == 2
    assert lead.contact.phone == "+12025550100"


def test_update_phone_only_bumps_updated_at(agent_client, lead):
    original_updated_at = lead.contact.updated_at
    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": "305-555-0199"})
    assert resp.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.updated_at > original_updated_at
