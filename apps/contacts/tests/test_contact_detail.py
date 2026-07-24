import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AddressFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def test_contact_detail_renders(client):
    c = ContactFactory(name="Priya")
    client.force_login(UserFactory())
    resp = client.get(reverse("contact_detail", args=[c.pk]))
    assert resp.status_code == 200
    assert b"Priya" in resp.content


def test_contact_update_writes_fields_and_resolves_company(client):
    c = ContactFactory(name="Priya", company=None)
    client.force_login(UserFactory())
    resp = client.post(
        reverse("contact_update", args=[c.pk]),
        {"name": "Priya A", "company": "Anand Family Office"},
    )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    c.refresh_from_db()
    assert c.name == "Priya A"
    assert c.company.name == "Anand Family Office"


def test_contact_update_rejects_invalid_phone(client):
    c = ContactFactory()
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[c.pk]), {"phone": "12345"})
    assert resp.status_code == 400
    assert "valid phone" in resp.json()["error"]


def test_contact_update_rejects_duplicate_email(client):
    ContactFactory(email="taken@example.com")
    c = ContactFactory(email="me@example.com")
    client.force_login(UserFactory())
    resp = client.post(reverse("contact_update", args=[c.pk]), {"email": "TAKEN@example.com"})
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()


def test_order_history_rows_carry_trip_count(logged_in_client):
    contact = ContactFactory()
    lead = LeadFactory(contact=contact)
    ReservationFactory.create_batch(2, lead=lead)
    resp = logged_in_client.get(reverse("contact_detail", args=[contact.pk]))
    assert resp.status_code == 200
    leads = resp.context["leads"]
    assert len(leads) == 1
    assert leads[0].trip_count == 2


def test_contact_detail_query_count_flat_across_leads(logged_in_client):
    def query_count(pk):
        with CaptureQueriesContext(connection) as ctx:
            resp = logged_in_client.get(reverse("contact_detail", args=[pk]))
            assert resp.status_code == 200
        return len(ctx)

    small = ContactFactory()
    ReservationFactory(lead=LeadFactory(contact=small))
    big = ContactFactory()
    for _ in range(3):
        ReservationFactory.create_batch(2, lead=LeadFactory(contact=big))
    assert query_count(big.pk) == query_count(small.pk)


def test_smart_address_view_block_renders_formatted_address(logged_in_client):
    addr = AddressFactory(
        line1="123 Main St",
        line2="Suite 400",
        city="Boston",
        state="MA",
        postal="02110",
        country="United States",
    )
    contact = ContactFactory(primary_address=addr)
    html = logged_in_client.get(reverse("contact_detail", args=[contact.pk])).content.decode()
    assert "123 Main St, Suite 400" in html
    assert "Boston, MA 02110" in html
    assert ">United States<" not in html  # US country line suppressed


def test_smart_address_view_block_shows_foreign_country(logged_in_client):
    addr = AddressFactory(line1="5 Rue Cler", city="Paris", country="France")
    contact = ContactFactory(primary_address=addr)
    html = logged_in_client.get(reverse("contact_detail", args=[contact.pk])).content.decode()
    assert ">France<" in html


def test_smart_address_view_block_empty_address(logged_in_client):
    contact = ContactFactory(primary_address=None)
    html = logged_in_client.get(reverse("contact_detail", args=[contact.pk])).content.decode()
    assert "No address on file" in html
