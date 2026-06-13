import pytest
from django.urls import reverse

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def agent(django_user_model):
    return django_user_model.objects.create_user(username="agent", password="pw")


def test_lead_list_requires_login(client):
    resp = client.get(reverse("lead_list"))
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_lead_list_shows_lead_quote_number(client, agent):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    client.force_login(agent)
    resp = client.get(reverse("lead_list"))
    assert resp.status_code == 200
    assert lead.quote_no.encode() in resp.content


def test_lead_list_filters_by_status(client, agent):
    LeadFactory(status=Lead.Status.NEW)
    quoted = LeadFactory(status=Lead.Status.QUOTED)
    client.force_login(agent)
    resp = client.get(reverse("lead_list"), {"status": "quoted"})
    leads = list(resp.context["leads"])
    assert quoted in leads
    assert all(lead.status == Lead.Status.QUOTED for lead in leads)


def test_lead_list_search_matches_contact_name(client, agent):
    match = LeadFactory(contact=ContactFactory(name="Wedding Wanda"))
    LeadFactory(contact=ContactFactory(name="Someone Else"))
    client.force_login(agent)
    resp = client.get(reverse("lead_list"), {"q": "wanda"})
    leads = list(resp.context["leads"])
    assert leads == [match]


def test_lead_detail_shows_reservations_and_total(client, agent):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    TransferReservationFactory(lead=lead, base_rate=200)
    TransferReservationFactory(lead=lead, base_rate=150)
    client.force_login(agent)
    resp = client.get(reverse("lead_detail", args=[lead.pk]))
    assert resp.status_code == 200
    assert resp.context["lead"] == lead
    assert resp.context["lead"].reservation_count == 2


def test_lead_detail_404_for_missing(client, agent):
    client.force_login(agent)
    resp = client.get(reverse("lead_detail", args=[999999]))
    assert resp.status_code == 404
