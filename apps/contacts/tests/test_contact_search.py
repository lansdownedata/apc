"""Customer lookup behind the New booking / New lead modal.

Two jobs on one endpoint: `?q=` feeds the type-ahead dropdown, and `?phone=&email=`
answers "does what they just typed already exist?" for the inline hint. The hint is
server-side on purpose — `Contact.objects.find_match` owns the E.164-vs-raw rules and
the browser must not re-implement them.
"""

import json

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.leads.factories import LeadFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _search(client, **params) -> dict:
    resp = client.get(reverse("contact_search"), params)
    assert resp.status_code == 200
    return json.loads(resp.content)


def _names(payload: dict) -> list[str]:
    return [r["name"] for r in payload["results"]]


def test_search_requires_login(client):
    resp = client.get(reverse("contact_search"), {"q": "anything"})
    assert resp.status_code == 302
    assert "/login/" in resp.url


def test_finds_a_contact_by_partial_name(staff):
    ContactFactory(name="James Okafor")
    ContactFactory(name="Sandra Vale")
    assert _names(_search(staff, q="okaf")) == ["James Okafor"]


def test_finds_a_contact_by_company_name(staff):
    ContactFactory(name="Dana Reed", company=CompanyFactory(name="Acme Logistics"))
    ContactFactory(name="Other Person")
    assert _names(_search(staff, q="acme")) == ["Dana Reed"]


def test_finds_a_contact_by_email(staff):
    ContactFactory(name="Ivy Chen", email="ivy@northwind.com")
    ContactFactory(name="Not Them", email="someone@else.com")
    assert _names(_search(staff, q="northwind")) == ["Ivy Chen"]


def test_finds_a_contact_by_formatted_phone(staff):
    """Phones are stored E.164; the agent types them the way a human writes them."""
    ContactFactory(name="Ray Diaz", phone="+16175559271")
    ContactFactory(name="Wrong Number", phone="+12025550100")
    assert _names(_search(staff, q="(617) 555-9271")) == ["Ray Diaz"]


def test_blank_query_returns_no_results(staff):
    ContactFactory(name="Somebody Real")
    assert _search(staff, q="  ")["results"] == []


def test_results_are_capped(staff):
    for i in range(12):
        ContactFactory(name=f"Rider {i}")
    assert len(_search(staff, q="Rider")["results"]) == 8


def test_a_result_carries_the_details_the_modal_fills_in(staff):
    contact = ContactFactory(
        name="Nina Patel",
        company=CompanyFactory(name="Vertex Group"),
        phone="+13015550188",
        email="nina@vertex.com",
    )
    LeadFactory(contact=contact)
    LeadFactory(contact=contact)
    (row,) = _search(staff, q="nina")["results"]
    assert row == {
        "id": contact.pk,
        "name": "Nina Patel",
        "company": "Vertex Group",
        "phone": "+13015550188",
        "email": "nina@vertex.com",
        "leads": 2,
    }


def test_a_contact_with_no_company_or_email_still_serializes(staff):
    ContactFactory(name="Bare Minimum", company=None, email=None, phone="")
    (row,) = _search(staff, q="Bare")["results"]
    assert row["company"] == "" and row["email"] == "" and row["phone"] == ""


def test_search_includes_contacts_with_no_leads(staff):
    """Someone who only ever texted Podium is still a bookable customer."""
    ContactFactory(name="Texter Only")
    assert _names(_search(staff, q="Texter")) == ["Texter Only"]


# ---------------------------------------------------------------- the inline hint


def test_a_typed_phone_that_already_exists_comes_back_as_a_match(staff):
    contact = ContactFactory(name="Repeat Customer", phone="+16175559271")
    assert _search(staff, phone="(617) 555-9271")["match"]["id"] == contact.pk


def test_a_typed_email_that_already_exists_comes_back_as_a_match(staff):
    contact = ContactFactory(name="Repeat Customer", email="repeat@example.com")
    assert _search(staff, email="REPEAT@example.com")["match"]["id"] == contact.pk


def test_an_unknown_phone_has_no_match(staff):
    ContactFactory(phone="+16175559271")
    assert _search(staff, phone="+15085550000")["match"] is None


def test_the_dropdown_query_alone_never_reports_a_match(staff):
    """`q` is a fuzzy search; only an exact phone/email is confident enough to auto-link."""
    ContactFactory(name="James Okafor")
    assert _search(staff, q="james")["match"] is None


def test_the_match_hint_is_absent_when_nothing_is_typed(staff):
    ContactFactory(name="Anyone")
    assert _search(staff, q="")["match"] is None


def test_search_manager_method_is_reusable(staff):
    """The directory and this endpoint must agree on what 'matches' means."""
    ContactFactory(name="Shared Logic")
    matched = list(Contact.objects.search("shared"))
    assert matched == list(Contact.objects.filter(name="Shared Logic"))
    assert list(Contact.objects.search("")) == []
