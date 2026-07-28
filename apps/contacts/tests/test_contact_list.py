"""Contacts directory — LTV aggregation (no join-multiplication), trips, search, totals."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import ConversationFactory, MessageFactory
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

    resp = logged_in_client.get(reverse("contact_list"))
    row = _row_for(resp.context["contacts"], contact)

    # 1000 + 500 only — not 1000*3 + 500*2, and the lost 9999 excluded.
    assert row.lifetime_value == Decimal("1500.00")
    # trips = 5 distinct reservations across the two booked leads (no double-count).
    assert row.trips == 5


def test_contact_with_no_leads_has_zero_ltv(logged_in_client):
    """Lead-less contacts are only listed under the All-contacts scope."""
    contact = ContactFactory()
    resp = logged_in_client.get(reverse("contact_list"), {"scope": "all"})
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
    resp = logged_in_client.get(reverse("contact_list"))
    assert _row_for(resp.context["contacts"], contact).trips == 3


@pytest.mark.parametrize(
    "field, value, term",
    [
        ("name", "Zephyr Aurelius", "zephyr"),
        ("company", "Quixotic Ventures LLC", "quixotic"),
        ("phone", "+16175559271", "(617) 555-9271"),
        ("email", "needle@haystack.example", "needle@haystack"),
    ],
)
def test_search_matches_each_field(logged_in_client, field, value, term):
    kwargs = {field: value}
    if field == "company":
        kwargs["company"] = CompanyFactory(name=value)
    match = ContactFactory(**kwargs)
    other = ContactFactory(
        name="Other Person",
        company=None,
        phone="(000) 000-0000",
        email="other@example.com",
    )
    # The directory lists customers by default, so both need a lead to be searchable.
    LeadFactory(contact=match)
    LeadFactory(contact=other)
    resp = logged_in_client.get(reverse("contact_list"), {"q": term})
    pks = {c.pk for c in resp.context["contacts"]}
    assert match.pk in pks
    assert other.pk not in pks


def test_header_totals(logged_in_client):
    c1 = ContactFactory()
    booked = LeadFactory(contact=c1, status=Lead.Status.BOOKED)
    PaymentPlanFactory(lead=booked, quote_total=Decimal("1200.00"))
    ContactFactory()  # second contact, no leads → $0
    resp = logged_in_client.get(reverse("contact_list"), {"scope": "all"})
    assert resp.context["total_contacts"] == 2
    assert resp.context["total_ltv"] == Decimal("1200.00")


def test_row_link_targets_contact_profile(logged_in_client):
    contact = ContactFactory()
    LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    newest = LeadFactory(contact=contact, status=Lead.Status.NEW)
    resp = logged_in_client.get(reverse("contact_list"))
    row = _row_for(resp.context["contacts"], contact)
    assert row.latest_lead_id == newest.pk
    assert reverse("contact_detail", args=[contact.pk]) in resp.content.decode()


def test_last_activity_uses_latest_of_lead_and_message(logged_in_client):
    contact = ContactFactory()
    lead = LeadFactory(contact=contact, status=Lead.Status.QUOTED)
    msg = MessageFactory(lead=lead)
    resp = logged_in_client.get(reverse("contact_list"))
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
    resp = logged_in_client.get(reverse("contact_list"))
    ordered = [c.pk for c in resp.context["contacts"]]
    assert ordered.index(active.pk) < ordered.index(quiet.pk)


def test_requires_login(client):
    resp = client.get(reverse("contact_list"))
    assert resp.status_code == 302
    assert "/login" in resp["Location"]


def test_directory_hides_contacts_with_no_leads(logged_in_client):
    """A contact with no leads has no LTV, no trips, and nothing the columns are for.

    Every stranger who texts the main business number becomes a Contact, so without
    this the directory fills with wrong numbers named +15715550137.
    """
    customer = ContactFactory(name="Real Customer")
    LeadFactory(contact=customer)
    ContactFactory(name="Wrong Number")  # no lead

    resp = logged_in_client.get(reverse("contact_list"))

    names = [c.name for c in resp.context["contacts"]]
    assert "Real Customer" in names
    assert "Wrong Number" not in names


def test_all_scope_shows_lead_less_contacts(logged_in_client):
    ContactFactory(name="Wrong Number")

    resp = logged_in_client.get(reverse("contact_list"), {"scope": "all"})

    assert "Wrong Number" in [c.name for c in resp.context["contacts"]]


def test_customer_with_several_leads_is_listed_once(logged_in_client):
    """The filter must be an EXISTS, not a join — a join lists them once per lead."""
    customer = ContactFactory(name="Repeat Customer")
    LeadFactory(contact=customer)
    LeadFactory(contact=customer)
    LeadFactory(contact=customer)

    resp = logged_in_client.get(reverse("contact_list"))

    names = [c.name for c in resp.context["contacts"]]
    assert names.count("Repeat Customer") == 1


def test_last_activity_uses_conversation_messages(logged_in_client):
    customer = ContactFactory()
    LeadFactory(contact=customer)
    convo = ConversationFactory(contact=customer)
    MessageFactory(conversation=convo, lead=None)

    resp = logged_in_client.get(reverse("contact_list"))

    assert _row_for(resp.context["contacts"], customer).last_message_at is not None


def test_contact_with_a_lead_but_no_conversation_renders(logged_in_client):
    """A contact from the New Lead modal has no Conversation at all.

    `contact.conversation` is a reverse OneToOne — reading it when absent raises
    RelatedObjectDoesNotExist, so nothing may touch it directly.
    """
    customer = ContactFactory(name="Modal Customer")
    lead = LeadFactory(contact=customer)

    resp = logged_in_client.get(reverse("contact_list"))
    assert resp.status_code == 200
    assert _row_for(resp.context["contacts"], customer).last_message_at is None

    assert logged_in_client.get(reverse("lead_detail", args=[lead.pk])).status_code == 200
