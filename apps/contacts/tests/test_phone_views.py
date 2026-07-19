"""Views for adding/removing/promoting a contact's phone numbers.

Object-level authz matters here: every phone view scopes its lookup by
`contact=contact` so acting on another contact's phone 404s rather than succeeding.
"""

import pytest
from django.contrib.messages import get_messages
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
    """`contact_phone_add` is posted by a plain HTML form (no fetch interception) —
    so a rejection must redirect back with a `django.contrib.messages` error, not
    dump raw JSON into the browser and lose the page. Nothing is added either way."""
    contact = ContactFactory(phone="(202) 555-0100")
    resp = agent_client.post(
        reverse("contact_phone_add", args=[contact.pk]), {"phone": "junk", "label": ""}
    )
    assert resp.status_code == 302
    assert resp["Location"] == reverse("contact_detail", args=[contact.pk])
    messages = [str(m) for m in get_messages(resp.wsgi_request)]
    assert "Enter a valid phone number." in messages
    assert contact.phones.count() == 1

    # And the contact_detail page actually renders it — no JSON dumped into the page.
    followed = agent_client.post(
        reverse("contact_phone_add", args=[contact.pk]),
        {"phone": "junk", "label": ""},
        follow=True,
    )
    assert "Enter a valid phone number." in followed.content.decode()


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


def test_delete_primary_promotes_the_remaining_number(agent_client):
    """Deleting the primary must not leave the contact with zero primaries — the
    invariant "exactly one primary whenever phones exist" must always hold, or
    downstream code that filters by `is_primary=True` (e.g. `lead_update`) silently
    no-ops instead of acting on the phone that's actually there."""
    contact = ContactFactory(phone="(202) 555-0100")
    primary = contact.primary_phone
    secondary = contact.add_phone("(305) 555-0199", label="work")
    agent_client.post(reverse("contact_phone_delete", args=[contact.pk, primary.pk]))
    secondary.refresh_from_db()
    assert contact.phones.count() == 1
    assert secondary.is_primary is True
    assert contact.phone == "+13055550199"


def test_delete_non_primary_leaves_the_primary_alone(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    primary = contact.primary_phone
    secondary = contact.add_phone("(305) 555-0199", label="work")
    agent_client.post(reverse("contact_phone_delete", args=[contact.pk, secondary.pk]))
    primary.refresh_from_db()
    assert contact.phones.count() == 1
    assert primary.is_primary is True


def test_delete_the_only_phone_leaves_zero_phones(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    primary = contact.primary_phone
    agent_client.post(reverse("contact_phone_delete", args=[contact.pk, primary.pk]))
    assert contact.phones.count() == 0


def test_delete_primary_then_lead_update_clear_ends_with_zero_phones(agent_client):
    """Regression for the end-to-end scenario: contact has A(primary) + B. Delete A
    on the contact page (must promote B to primary). Then clear the phone via
    `lead_update` — its `phones.filter(is_primary=True).delete()` must actually hit
    B, not silently match nothing and report success while B is still there."""
    from apps.leads.factories import LeadFactory

    contact = ContactFactory(phone="(202) 555-0100")
    a = contact.primary_phone
    contact.add_phone("(305) 555-0199", label="work")
    lead = LeadFactory(contact=contact)

    agent_client.post(reverse("contact_phone_delete", args=[contact.pk, a.pk]))
    assert contact.phones.count() == 1

    resp = agent_client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    contact.refresh_from_db()
    assert contact.phones.count() == 0
    assert contact.phone == ""


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


def test_cannot_promote_another_contacts_phone(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0199")
    foreign = other.phones.first()
    resp = agent_client.post(reverse("contact_phone_primary", args=[contact.pk, foreign.pk]))
    assert resp.status_code == 404
    foreign.refresh_from_db()
    assert foreign.contact_id == other.pk
    assert foreign.is_primary is True


def test_add_phone_already_claimed_by_another_contact(agent_client):
    contact = ContactFactory(phone="(202) 555-0100")
    other = ContactFactory(phone="(305) 555-0299")
    resp = agent_client.post(
        reverse("contact_phone_add", args=[other.pk]),
        {"phone": "202.555.0100", "label": ""},
    )
    assert resp.status_code == 302
    assert resp["Location"] == reverse("contact_detail", args=[other.pk])
    messages = [str(m) for m in get_messages(resp.wsgi_request)]
    assert "That number is already assigned to another contact." in messages
    assert other.phones.count() == 1
    assert contact.phones.filter(e164="+12025550100").exists()
