"""The office's side of a wedding: the details card, Edit details, and New wedding.

A wedding lead holds the couple's answers and no generated trips. The workspace shows
every answer by category, Edit details changes them, and an agent builds the trips by
hand in the ordinary trip editor.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.public.forms import WeddingRequestForm
from apps.public.services import create_lead_from_wedding
from apps.public.tests.test_wedding_details import _post
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _portal(**over):
    data = _post(**over)
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)
    return data


def _wedding_lead(**over) -> Lead:
    form = WeddingRequestForm(_post(**over))
    assert form.is_valid(), form.errors
    return create_lead_from_wedding(form.cleaned_data)


def _workspace(client, lead, query="") -> str:
    return client.get(f"{reverse('lead_detail', args=[lead.pk])}{query}").content.decode()


@pytest.fixture
def agent(client):
    user = UserFactory()
    client.force_login(user)
    return user


# --- the details card --------------------------------------------------------------


def test_a_trip_less_wedding_shows_every_answer_by_category(client, agent):
    html = _workspace(client, _wedding_lead())
    for text in (
        "Venue &amp; ceremony",
        "Ceremony location",
        "St. John&#x27;s Church",
        "Guests riding the shuttle",
        "Lansdowne Resort",
        "Venue requires everyone out by",
        "Bridesmaids get ready",
    ):
        assert text in html, text


def test_the_button_says_edit_details(client, agent):
    html = _workspace(client, _wedding_lead())
    assert "Edit details" in html
    assert "Edit the day" not in html


def test_a_trip_less_wedding_still_gets_the_day_of_card(client, agent):
    lead = _wedding_lead()
    assert client.get(reverse("lead_detail", args=[lead.pk])).context["is_wedding"] is True


def test_the_unconfirmed_flags_lead_the_card(client, agent):
    lead = _wedding_lead(
        hotels_json="[]", hotels_tbd="1", ceremony_time="", end_time="", times_tbd="1"
    )
    html = _workspace(client, lead)
    assert "times not set" in html
    assert "hotels not booked" in html


def test_a_wedding_saved_by_the_old_builder_still_reads_back(client, agent):
    """Its payload carries `legs`; they are ignored, the answers are not."""
    lead = _wedding_lead()
    lead.intake_payload = {**lead.intake_payload, "legs": [{"id": "guests-in"}]}
    lead.save(update_fields=["intake_payload"])
    assert "Guests riding the shuttle" in _workspace(client, lead)


def test_an_ordinary_lead_has_no_wedding_card(client, agent):
    html = _workspace(client, LeadFactory())
    assert "Edit details" not in html
    assert "weddingPlanner(" not in html


# --- Edit details ------------------------------------------------------------------


def test_the_editor_shows_every_category_and_no_itinerary(client, agent):
    html = _workspace(client, _wedding_lead())
    assert "weddingPlanner(" in html
    assert "portal: true" in html
    assert reverse("lead_wedding_save", args=[1]).rsplit("/", 3)[0] in html
    assert "Save details" in html
    for gone in ("legs_json", "vehicles_json", "Save the day", "Reset to our suggestion"):
        assert gone not in html, gone


def test_the_save_route_requires_login(client):
    lead = LeadFactory()
    resp = client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    assert "/portal/login/" in resp["Location"]


def test_saving_updates_the_answers_and_nothing_else(client, agent):
    lead = _wedding_lead()
    trip = ReservationFactory(lead=lead)
    Lead.objects.filter(pk=lead.pk).update(notes="Called Jane — wants a trolley.")
    resp = client.post(
        reverse("lead_wedding_save", args=[lead.pk]),
        _portal(guest_count="140", notes="Grand exit at 10:45."),
    )
    assert resp["Location"] == reverse("lead_detail", args=[lead.pk])
    lead.refresh_from_db()
    assert lead.intake_payload["guest_count"] == 140
    assert lead.intake_payload["notes"] == "Grand exit at 10:45."
    assert lead.notes == "Called Jane — wants a trolley."
    assert list(lead.reservations.all()) == [trip]


def test_saving_keeps_the_contact_the_lead_already_has(client, agent):
    lead = _wedding_lead()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal())
    lead.refresh_from_db()
    assert lead.contact.name == "Jane Rider"
    assert lead.intake_payload["email"] == "jane@example.com"


def test_an_invalid_save_changes_nothing_and_says_why(client, agent):
    lead = _wedding_lead()
    resp = client.post(
        reverse("lead_wedding_save", args=[lead.pk]), _portal(groups=""), follow=True
    )
    lead.refresh_from_db()
    assert lead.intake_payload["groups"] == ["guests", "party"]
    assert "Tell us who needs a ride" in resp.content.decode()


def test_a_wedding_moved_inside_the_window_raises_the_alert(client, agent):
    from datetime import timedelta

    from django.utils import timezone

    lead = _wedding_lead()
    assert lead.has_alert is False
    soon = (timezone.localdate() + timedelta(days=10)).isoformat()
    client.post(reverse("lead_wedding_save", args=[lead.pk]), _portal(wedding_date=soon))
    lead.refresh_from_db()
    assert lead.has_alert is True


# --- New wedding -------------------------------------------------------------------


def _new_wedding(client) -> Lead:
    resp = client.post(
        reverse("lead_create"),
        {
            "name": "Priya Shah",
            "phone": "",
            "email": "priya@example.com",
            "channel": "phone",
            "intent": "wedding",
        },
    )
    lead = Lead.objects.get(contact__name="Priya Shah")
    assert resp["Location"] == f"{reverse('lead_detail', args=[lead.pk])}?wedding=1"
    return lead


def test_new_wedding_opens_straight_on_edit_details(client, agent):
    lead = _new_wedding(client)
    resp = client.get(f"{reverse('lead_detail', args=[lead.pk])}?wedding=1")
    assert resp.context["wedding_open"] is True


def test_a_new_wedding_is_a_wedding_even_if_the_agent_closes_the_editor(client, agent):
    """The card, and the way back into Edit details, must not depend on a first save."""
    html = _workspace(client, _new_wedding(client))
    assert "Edit details" in html
    assert "no details yet" in html


def test_a_new_wedding_skips_the_website_worded_welcome(client, agent):
    lead = _new_wedding(client)
    assert not TouchPoint.objects.filter(lead=lead).exists()


def test_the_contact_modal_speaks_wedding(client, agent):
    html = client.get(reverse("lead_list")).content.decode()
    assert "Create wedding" in html
    assert "opens the wedding details" in html


# --- building the trips by hand ----------------------------------------------------


def test_a_new_trip_on_a_wedding_starts_on_the_wedding_date_and_occasion(client, agent):
    ServiceTypeFactory(name="Wedding Transportation")
    lead = _wedding_lead()
    defaults = client.get(reverse("lead_detail", args=[lead.pk])).context["trip_defaults"]
    assert defaults["date"] == lead.intake_payload["wedding_date"]
    assert defaults["serviceType"]


def test_an_ordinary_lead_gets_no_trip_defaults(client, agent):
    lead = LeadFactory()
    assert client.get(reverse("lead_detail", args=[lead.pk])).context["trip_defaults"] == {}
