import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
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


def test_lead_detail_shows_ledger_and_balances(client, agent):
    from decimal import Decimal

    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    lead = LeadFactory(status=Lead.Status.BOOKED)
    ledger.post_capture(
        lead=lead, amount=Decimal("1000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap-x",
    )
    client.force_login(agent)
    resp = client.get(reverse("lead_detail", args=[lead.pk]))
    assert resp.status_code == 200
    assert resp.context["balances"]["collected"] == Decimal("1000.00")
    assert len(resp.context["ledger_entries"]) == 1


def test_lead_create_makes_lead_and_contact(client):
    client.force_login(UserFactory())
    resp = client.post(
        reverse("lead_create"),
        {"name": "Sarah Boyne", "company": "", "phone": "(703) 555-0148",
         "email": "sarah@example.com", "channel": "phone", "agent": ""},
    )
    assert resp.status_code == 302
    lead = Lead.objects.get()
    assert lead.contact.name == "Sarah Boyne"
    assert lead.channel == "phone"
    assert resp.url == reverse("lead_detail", args=[lead.pk])


def test_lead_create_dedupes_contact(client):
    existing = ContactFactory(phone="(703) 555-0148", email="old@example.com")
    client.force_login(UserFactory())
    client.post(
        reverse("lead_create"),
        {"name": "Sarah B", "phone": "(703) 555-0148", "email": "new@example.com",
         "channel": "website", "agent": ""},
    )
    assert Contact.objects.count() == 1
    assert Lead.objects.get().contact == existing


def test_lead_create_requires_name(client):
    client.force_login(UserFactory())
    resp = client.post(reverse("lead_create"), {"name": "", "channel": "website"})
    assert resp.status_code == 302
    assert Lead.objects.count() == 0


def test_lead_create_requires_login(client):
    resp = client.post(reverse("lead_create"), {"name": "X"})
    assert resp.status_code == 302
    assert "/login" in resp.url


def test_lead_update_writes_contact_and_lead(client):
    lead = LeadFactory(channel="website")
    agent = UserFactory()
    client.force_login(UserFactory())
    resp = client.post(
        reverse("lead_update", args=[lead.pk]),
        {"name": "New Name", "phone": "(202) 555-0001", "email": "n@example.com",
         "company": "Acme", "channel": "phone", "agent": str(agent.pk)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    lead.refresh_from_db()
    lead.contact.refresh_from_db()
    assert lead.contact.name == "New Name"
    assert lead.contact.company == "Acme"
    assert lead.channel == "phone"
    assert lead.assigned_agent_id == agent.pk


def test_lead_update_clears_agent_when_blank(client):
    agent = UserFactory()
    lead = LeadFactory(assigned_agent=agent)
    client.force_login(UserFactory())
    client.post(reverse("lead_update", args=[lead.pk]), {"agent": ""})
    lead.refresh_from_db()
    assert lead.assigned_agent_id is None


def test_lead_update_requires_login(client):
    lead = LeadFactory()
    resp = client.post(reverse("lead_update", args=[lead.pk]), {"name": "x"})
    assert resp.status_code == 302
