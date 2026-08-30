"""Linking a new lead/booking to an existing customer from the contact modal.

Picking a customer in the modal posts `contact_id`. That contact is reused as-is — no
dedupe guessing — and whatever the agent edited in the modal is written back to the
profile. A blank field is "I didn't fill this in", never "erase what's on file"; the
place to clear a value is the contact profile.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _post(client, follow=False, **over):
    data = {"name": "Ada Byron", "channel": "website", "intent": "booking"}
    data.update(over)
    return client.post(reverse("lead_create"), data, follow=follow)


def test_a_picked_customer_is_reused_instead_of_creating_a_new_one(staff):
    contact = ContactFactory(name="Ada Byron", phone="+12025550100")
    before = Contact.objects.count()

    _post(staff, contact_id=contact.pk, name="Ada Byron", phone="+12025550100")

    assert Contact.objects.count() == before
    assert Lead.objects.latest("id").contact == contact


def test_a_picked_customer_is_reused_even_when_nothing_identifies_them(staff):
    """No phone and no email means `match_or_create` would have made a duplicate."""
    contact = ContactFactory(name="Walk In", phone="", email=None)
    before = Contact.objects.count()

    _post(staff, contact_id=contact.pk, name="Walk In", phone="", email="")

    assert Contact.objects.count() == before
    assert Lead.objects.latest("id").contact == contact


def test_edits_in_the_modal_update_the_customers_profile(staff):
    contact = ContactFactory(name="Ada Byron", phone="+12025550100", email="ada@old.com")

    _post(
        staff,
        contact_id=contact.pk,
        name="Ada Lovelace",
        phone="+12025559999",
        email="ada@new.com",
        company="Analytical Engines",
    )

    contact.refresh_from_db()
    assert contact.name == "Ada Lovelace"
    assert contact.phone == "+12025559999"
    assert contact.email == "ada@new.com"
    assert contact.company.name == "Analytical Engines"


def test_a_blank_field_leaves_the_stored_value_alone(staff):
    contact = ContactFactory(
        name="Ada Byron",
        phone="+12025550100",
        email="ada@example.com",
        company=CompanyFactory(name="Analytical Engines"),
    )

    _post(staff, contact_id=contact.pk, name="Ada Byron", phone="", email="", company="")

    contact.refresh_from_db()
    assert contact.phone == "+12025550100"
    assert contact.email == "ada@example.com"
    assert contact.company.name == "Analytical Engines"


def test_the_lead_source_does_not_overwrite_how_the_customer_found_us(staff):
    contact = ContactFactory(name="Ada Byron", channel=Channel.WEDDING_PRO)

    _post(staff, contact_id=contact.pk, channel="phone")

    contact.refresh_from_db()
    assert contact.channel == Channel.WEDDING_PRO
    assert Lead.objects.latest("id").channel == "phone"


def test_an_email_belonging_to_someone_else_is_refused_without_losing_the_booking(staff):
    ContactFactory(name="Other Person", email="taken@example.com")
    contact = ContactFactory(name="Ada Byron", email="ada@example.com")

    resp = _post(staff, contact_id=contact.pk, email="taken@example.com", follow=True)

    contact.refresh_from_db()
    assert contact.email == "ada@example.com"
    assert Lead.objects.filter(contact=contact).exists()
    assert "another customer" in " ".join(m.message for m in resp.context["messages"]).lower()


def test_an_unknown_contact_id_is_rejected(staff):
    resp = _post(staff, contact_id=999999)
    assert resp.status_code == 302
    assert resp.url == reverse("lead_list")
    assert not Lead.objects.exists()


def test_without_a_pick_the_old_dedupe_still_applies_and_never_renames_anyone(staff):
    """No `contact_id`: phone/email still attaches to a match, but the typed name is
    NOT written back — the agent never confirmed these are the same person."""
    contact = ContactFactory(name="Ada Byron", phone="+12025550100")
    before = Contact.objects.count()

    _post(staff, name="A. Byron-Lovelace", phone="+12025550100")

    contact.refresh_from_db()
    assert Contact.objects.count() == before
    assert contact.name == "Ada Byron"
    assert Lead.objects.latest("id").contact == contact


def test_the_modal_carries_the_customer_search(staff):
    html = staff.get(reverse("lead_list")).content.decode()
    assert reverse("contact_search") in html  # the type-ahead's endpoint
    assert 'name="contact_id"' in html  # what a pick posts
    assert "contactPicker(" in html  # the Alpine component driving it


def test_the_orders_console_modal_carries_it_too(staff):
    html = staff.get(reverse("orders_list")).content.decode()
    assert reverse("contact_search") in html
    assert 'name="contact_id"' in html
