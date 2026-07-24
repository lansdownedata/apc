import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.forms import NewLeadForm

pytestmark = pytest.mark.django_db


def test_lead_update_stores_e164(client):
    lead = LeadFactory(channel="website")
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": "(617) 555-0207"})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+16175550207"


def test_lead_update_rejects_invalid_phone(client):
    lead = LeadFactory(channel="website")
    lead.contact.phone = "+12025550100"
    lead.contact.save(update_fields=["phone"])
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": "12345"})

    assert response.status_code == 400
    assert "valid phone" in response.json()["error"]
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+12025550100", "nothing may be written when validation fails"


def test_lead_update_allows_blank_phone(client):
    lead = LeadFactory(channel="website")
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"phone": ""})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == ""


def test_lead_update_without_phone_key_leaves_it_alone(client):
    lead = LeadFactory(channel="website")
    lead.contact.phone = "+16175550207"
    lead.contact.save(update_fields=["phone"])
    client.force_login(UserFactory())

    response = client.post(reverse("lead_update", args=[lead.pk]), {"name": "Ada"})

    assert response.status_code == 200
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+16175550207"


def test_new_lead_form_normalizes_phone():
    form = NewLeadForm({"name": "Ada", "phone": "(617) 555-0207", "channel": "website"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == "+16175550207"


def test_new_lead_form_rejects_invalid_phone():
    form = NewLeadForm({"name": "Ada", "phone": "12345", "channel": "website"})
    assert not form.is_valid()
    assert "phone" in form.errors


def test_new_lead_form_allows_blank_phone():
    form = NewLeadForm({"name": "Ada", "phone": "", "channel": "website"})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["phone"] == ""


def test_lead_update_rejects_duplicate_email(client):
    from apps.contacts.factories import ContactFactory

    ContactFactory(email="taken@example.com")
    lead = LeadFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("lead_update", args=[lead.pk]), {"email": "TAKEN@example.com"})
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()
