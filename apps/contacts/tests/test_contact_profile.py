"""The contact profile: phone numbers managed in place, and a richer read of the customer."""

import pytest
from django.urls import reverse

from apps.contacts import services
from apps.contacts.factories import ContactFactory
from apps.contacts.models import ContactPhone
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.factories import ConversationFactory, MessageFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def jane():
    return ContactFactory(name="Jane Rider", phone="+16175550207", email="jane@example.com")


def _numbers(resp):
    return [(p["number"], p["label"], p["texting"]) for p in resp.json()["phones"]]


# --- the phone endpoints ------------------------------------------------------------


def test_adding_a_number_returns_the_whole_list(logged_in_client, jane):
    resp = logged_in_client.post(
        reverse("contact_phone_add", args=[jane.pk]), {"number": "(202) 555-0134", "label": "work"}
    )
    assert resp.status_code == 200
    assert _numbers(resp) == [("+16175550207", "mobile", True), ("+12025550134", "work", False)]
    assert resp.json()["phones"][1]["display"] == "(202) 555-0134"


def test_adding_an_undiallable_number_is_refused(logged_in_client, jane):
    resp = logged_in_client.post(reverse("contact_phone_add", args=[jane.pk]), {"number": "12"})
    assert resp.status_code == 400
    assert "valid phone" in resp.json()["error"]


def test_an_unknown_label_falls_back_to_mobile(logged_in_client):
    contact = ContactFactory(phone="")
    resp = logged_in_client.post(
        reverse("contact_phone_add", args=[contact.pk]),
        {"number": "(202) 555-0134", "label": "pager"},
    )
    assert _numbers(resp) == [("+12025550134", "mobile", True)]


def test_editing_a_number(logged_in_client, jane):
    row = jane.phones.get()
    resp = logged_in_client.post(
        reverse("contact_phone_update", args=[jane.pk, row.pk]),
        {"number": "(202) 555-0134", "label": "home"},
    )
    assert _numbers(resp) == [("+12025550134", "home", True)]
    jane.refresh_from_db()
    assert jane.phone == "+12025550134"


def test_use_for_texting(logged_in_client, jane):
    work = services.add_phone(jane, "(202) 555-0134", label=ContactPhone.Label.WORK)
    resp = logged_in_client.post(reverse("contact_phone_texting", args=[jane.pk, work.pk]))
    assert _numbers(resp)[0] == ("+12025550134", "work", True)
    jane.refresh_from_db()
    assert jane.phone == "+12025550134"


def test_removing_the_texting_number_while_others_exist_is_refused(logged_in_client, jane):
    services.add_phone(jane, "(202) 555-0134")
    texting = jane.phones.get(texting=True)
    resp = logged_in_client.post(reverse("contact_phone_delete", args=[jane.pk, texting.pk]))
    assert resp.status_code == 400
    assert "texting" in resp.json()["error"]


def test_removing_another_number(logged_in_client, jane):
    work = services.add_phone(jane, "(202) 555-0134")
    resp = logged_in_client.post(reverse("contact_phone_delete", args=[jane.pk, work.pk]))
    assert _numbers(resp) == [("+16175550207", "mobile", True)]


def test_a_number_on_another_contact_is_out_of_reach(logged_in_client, jane):
    other = ContactFactory(phone="+12025550100")
    resp = logged_in_client.post(
        reverse("contact_phone_delete", args=[jane.pk, other.phones.get().pk])
    )
    assert resp.status_code == 404


def test_the_phone_endpoints_need_a_login(client, jane):
    resp = client.post(reverse("contact_phone_add", args=[jane.pk]), {"number": "2025550134"})
    assert resp.status_code == 302


# --- the page -----------------------------------------------------------------------


def test_the_profile_lists_every_number_and_marks_the_texting_one(logged_in_client, jane):
    services.add_phone(jane, "(202) 555-0134", label=ContactPhone.Label.WORK)
    html = logged_in_client.get(reverse("contact_detail", args=[jane.pk])).content.decode()
    assert "Phone numbers" in html
    assert "(617) 555-0207" in html and "(202) 555-0134" in html
    assert "Podium texting" in html
    assert "Add a number" in html


def test_the_numbers_reach_alpine_as_a_list_not_a_string(logged_in_client, jane):
    """json_string would hand Alpine a string, and x-for would loop over its characters."""
    html = logged_in_client.get(reverse("contact_detail", args=[jane.pk])).content.decode()
    assert "phones: [{&quot;id&quot;" in html
    assert "preset: {&quot;id&quot;" in html  # the New-lead modal opens linked to her


def test_the_header_leads_with_value_and_open_quotes(logged_in_client, jane):
    LeadFactory(contact=jane, status=Lead.Status.QUOTED)
    LeadFactory(contact=jane, status=Lead.Status.LOST)
    resp = logged_in_client.get(reverse("contact_detail", args=[jane.pk]))
    assert resp.context["stats"]["open_quotes"] == 1
    html = resp.content.decode()
    assert "Lifetime value" in html
    assert "Open quotes" in html
    assert "Customer since" in html


def test_the_latest_message_links_to_the_inbox_thread(logged_in_client, jane):
    convo = ConversationFactory(contact=jane)
    MessageFactory(conversation=convo, body="Can the sprinter fit 14?")
    html = logged_in_client.get(reverse("contact_detail", args=[jane.pk])).content.decode()
    assert "Can the sprinter fit 14?" in html
    assert f"{reverse('inbox')}?conversation={convo.pk}" in html


def test_no_conversation_means_no_message_card(logged_in_client, jane):
    html = logged_in_client.get(reverse("contact_detail", args=[jane.pk])).content.decode()
    assert "Latest message" not in html


def test_orders_and_quotes_can_be_filtered(logged_in_client, jane):
    LeadFactory(contact=jane, status=Lead.Status.BOOKED)
    html = logged_in_client.get(reverse("contact_detail", args=[jane.pk])).content.decode()
    assert "Orders &amp; quotes" in html
    assert "orderFilter" in html
