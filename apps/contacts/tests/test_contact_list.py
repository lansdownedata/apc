"""Contacts directory — LTV aggregation (no join-multiplication), trips, search, totals."""

from decimal import Decimal

import pytest

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import MessageFactory
from apps.payments.factories import PaymentPlanFactory
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _row_for(contacts, contact):
    return next(c for c in contacts if c.pk == contact.pk)


def test_ltv_sums_only_booked_plans_without_join_multiplication(logged_in_client):
    """The crux: two booked leads (with several reservations each) plus a lost lead.

    LTV must equal the sum of the *booked* plans' snapshot totals — NOT multiplied by
    the number of reservations, and excluding the lost lead's plan.
    """
    contact = ContactFactory()

    booked1 = LeadFactory(contact=contact, status=Lead.Status.BOOKED)
    PaymentPlanFactory(lead=booked1, quote_total=Decimal("1000.00"))
    ReservationFactory(lead=booked1)
    ReservationFactory(lead=booked1)
    ReservationFactory(lead=booked1)
    # Messages on the same lead: the reservations x messages cartesian must not
    # inflate the LTV Sum or the trips Count.
    MessageFactory(lead=booked1)
    MessageFactory(lead=booked1)

    booked2 = LeadFactory(contact=contact, status=Lead.Status.BOOKED)
    PaymentPlanFactory(lead=booked2, quote_total=Decimal("500.00"))
    ReservationFactory(lead=booked2)
    ReservationFactory(lead=booked2)

    lost = LeadFactory(contact=contact, status=Lead.Status.LOST)
    PaymentPlanFactory(lead=lost, quote_total=Decimal("9999.00"))

    resp = logged_in_client.get("/contacts/")
    row = _row_for(resp.context["contacts"], contact)

    # 1000 + 500 only — not 1000*3 + 500*2, and the lost 9999 excluded.
    assert row.lifetime_value == Decimal("1500.00")
    # trips = 5 distinct reservations across the two booked leads (no double-count).
    assert row.trips == 5


def test_contact_with_no_leads_has_zero_ltv(logged_in_client):
    contact = ContactFactory()
    resp = logged_in_client.get("/contacts/")
    row = _row_for(resp.context["contacts"], contact)
    assert row.lifetime_value == Decimal("0.00")
    assert row.trips == 0
    assert row.latest_lead_id is None
    # A lead-less contact renders a "New lead" affordance instead of a row link.
    assert "New lead" in resp.content.decode()


def test_trips_counts_reservations_across_leads(logged_in_client):
    contact = ContactFactory()
    lead_a = LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    lead_b = LeadFactory(contact=contact, status=Lead.Status.NEW)
    ReservationFactory(lead=lead_a)
    ReservationFactory(lead=lead_a)
    ReservationFactory(lead=lead_b)
    resp = logged_in_client.get("/contacts/")
    assert _row_for(resp.context["contacts"], contact).trips == 3


@pytest.mark.parametrize(
    "field, value, term",
    [
        ("name", "Zephyr Aurelius", "zephyr"),
        ("company", "Quixotic Ventures LLC", "quixotic"),
        ("phone", "(617) 555-9271", "555-9271"),
        ("email", "needle@haystack.example", "needle@haystack"),
    ],
)
def test_search_matches_each_field(logged_in_client, field, value, term):
    match = ContactFactory(**{field: value})
    other = ContactFactory(
        name="Other Person",
        company="",
        phone="(000) 000-0000",
        email="other@example.com",
    )
    resp = logged_in_client.get("/contacts/", {"q": term})
    pks = {c.pk for c in resp.context["contacts"]}
    assert match.pk in pks
    assert other.pk not in pks


def test_search_alphanumeric_text_does_not_over_match_via_digit_collapse(logged_in_client):
    """ "Suite 5" must not collapse to digit "5" and match any contact whose number
    contains a 5 — it should only match on the name/company/email fields."""
    match = ContactFactory(name="Suite 5 Events", phone="(202) 555-0187")
    other = ContactFactory(name="Random Corp", phone="(202) 555-0155")
    resp = logged_in_client.get("/contacts/", {"q": "Suite 5"})
    pks = {c.pk for c in resp.context["contacts"]}
    assert match.pk in pks
    assert other.pk not in pks


def test_header_totals(logged_in_client):
    c1 = ContactFactory()
    booked = LeadFactory(contact=c1, status=Lead.Status.BOOKED)
    PaymentPlanFactory(lead=booked, quote_total=Decimal("1200.00"))
    ContactFactory()  # second contact, no leads → $0
    resp = logged_in_client.get("/contacts/")
    assert resp.context["total_contacts"] == 2
    assert resp.context["total_ltv"] == Decimal("1200.00")


def test_row_link_targets_most_recent_lead(logged_in_client):
    contact = ContactFactory()
    LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    newest = LeadFactory(contact=contact, status=Lead.Status.NEW)
    resp = logged_in_client.get("/contacts/")
    row = _row_for(resp.context["contacts"], contact)
    assert row.latest_lead_id == newest.pk
    assert f"/leads/{newest.pk}/" in resp.content.decode()


def test_last_activity_uses_latest_of_lead_and_message(logged_in_client):
    contact = ContactFactory()
    lead = LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    msg = MessageFactory(lead=lead)
    resp = logged_in_client.get("/contacts/")
    row = _row_for(resp.context["contacts"], contact)
    assert row.last_activity is not None
    # message is created after the lead, so last_activity tracks the message.
    assert row.last_activity >= msg.created_at


def test_ordered_by_most_recent_activity_first(logged_in_client):
    quiet = ContactFactory(name="Quiet Contact")
    LeadFactory(contact=quiet, status=Lead.Status.NEW)
    active = ContactFactory(name="Active Contact")
    lead = LeadFactory(contact=active, status=Lead.Status.QUOTED)
    MessageFactory(lead=lead)  # freshest activity
    resp = logged_in_client.get("/contacts/")
    ordered = [c.pk for c in resp.context["contacts"]]
    assert ordered.index(active.pk) < ordered.index(quiet.pk)


def test_requires_login(client):
    resp = client.get("/contacts/")
    assert resp.status_code == 302
    assert "/login" in resp["Location"]
