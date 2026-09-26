"""The wedding intake collects DETAILS, not trips.

A couple answers the same questions as before, but nothing builds their day on the spot:
weddings run too many different ways for a rule to guess the legs. The lead arrives
holding every answer and no reservations, and one of our own people builds the trips.
"""

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.addresses.factories import VenueFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.public.forms import WeddingRequestForm
from apps.public.services import create_lead_from_wedding, make_wedding_token, wedding_payload
from apps.public.wedding import wedding_answers
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

PLAN_URL = "/weddings/plan/"


@pytest.fixture(autouse=True)
def _clear_throttle():
    cache.clear()


def _post(**over):
    data = {
        "name": "Jane Rider",
        "email": "jane@example.com",
        "phone": "",
        "wedding_date": (timezone.localdate() + timedelta(days=300)).isoformat(),
        "venue_name": "The Oak Barn at Loyalty",
        "same_site": "",
        "ceremony_venue_name": "St. John's Church",
        "groups": "guests,party",
        "guest_count": "105",
        "party_count": "14",
        "family_count": "8",
        "hotels_json": json.dumps(
            [
                {"venue_id": None, "name": "Hampton Inn Leesburg"},
                {"venue_id": None, "name": "Lansdowne Resort"},
            ]
        ),
        "ceremony_time": "16:00",
        "end_time": "23:00",
        "notes": "Bridesmaids get ready at an Airbnb in Purcellville.",
        "company": "",
    }
    data.update(over)
    return data


def _cleaned(**over) -> dict:
    form = WeddingRequestForm(_post(**over))
    assert form.is_valid(), form.errors
    return form.cleaned_data


def _answers(payload: dict) -> dict:
    """{category title: {question: answer}} — easier to assert on than the list."""
    return {c["title"]: dict(c["rows"]) for c in wedding_answers(payload)}


# --- the form ----------------------------------------------------------------------


def test_a_submission_needs_no_itinerary():
    assert "legs_json" not in WeddingRequestForm.base_fields
    assert WeddingRequestForm(_post()).is_valid()


def test_the_free_text_answer_is_kept_and_capped():
    assert _cleaned()["notes"].startswith("Bridesmaids get ready")
    assert not WeddingRequestForm(_post(notes="x" * 2001)).is_valid()


# --- every answer, by category -----------------------------------------------------


def test_every_answer_is_reported_under_its_category():
    answers = _answers(wedding_payload(_cleaned()))
    assert list(answers) == [
        "Date",
        "Venue & ceremony",
        "Who's riding",
        "Hotels",
        "Times",
        "Anything else",
    ]
    assert answers["Venue & ceremony"] == {
        "Reception venue": "The Oak Barn at Loyalty",
        "Ceremony at the same place?": "No — two locations",
        "Ceremony location": "St. John's Church",
    }
    assert answers["Who's riding"] == {
        "Who needs a ride?": "Our guests, The wedding party",
        "Guests riding the shuttle": "105",
        "Wedding party": "14",
    }
    assert answers["Hotels"] == {
        "Where is everyone staying?": "Hampton Inn Leesburg; Lansdowne Resort"
    }
    assert answers["Times"] == {
        "Ceremony starts": "4:00 PM",
        "Venue requires everyone out by": "11:00 PM",
    }
    assert answers["Anything else"]["Anything else we should know?"].startswith("Bridesmaids")


def test_a_count_is_only_reported_for_a_group_that_is_riding():
    """The family stepper keeps its default of 8 even when family isn't riding."""
    riding = _answers(wedding_payload(_cleaned()))["Who's riding"]
    assert "Family & VIPs" not in riding


def test_not_yet_is_an_answer_not_a_blank():
    answers = _answers(
        wedding_payload(
            _cleaned(hotels_json="[]", hotels_tbd="1", ceremony_time="", end_time="", times_tbd="1")
        )
    )
    assert answers["Hotels"] == {"Where is everyone staying?": "Not booked yet"}
    assert answers["Times"] == {
        "Ceremony starts": "Not set yet",
        "Venue requires everyone out by": "Not set yet",
    }


def test_the_hotel_question_is_dropped_when_nobody_rides_from_one():
    answers = _answers(wedding_payload(_cleaned(groups="couple", hotels_json="[]")))
    assert "Hotels" not in answers
    assert "Anything else" in answers


def test_every_hotel_keeps_its_own_address():
    """The old composite "2 hotels — A, B" stop threw away every address but the first."""
    first = VenueFactory(name="Hampton Inn Leesburg", address="117 Fort Evans Rd NE")
    second = VenueFactory(name="Lansdowne Resort", address="44050 Woodridge Pkwy")
    payload = wedding_payload(
        _cleaned(
            hotels_json=json.dumps(
                [
                    {"venue_id": first.pk, "name": first.name},
                    {"venue_id": second.pk, "name": second.name},
                ]
            )
        )
    )
    assert [h["address"] for h in payload["hotels"]] == [
        "117 Fort Evans Rd NE",
        "44050 Woodridge Pkwy",
    ]


# --- the lead ----------------------------------------------------------------------


def test_the_lead_arrives_with_the_details_and_no_trips():
    lead = create_lead_from_wedding(_cleaned())
    assert lead.status == Lead.Status.NEW
    assert lead.reservations.count() == 0
    assert lead.intake_payload["guest_count"] == 105
    assert lead.intake_payload["notes"].startswith("Bridesmaids")
    assert "legs" not in lead.intake_payload


def test_the_notes_carry_the_details_for_the_pipeline_card():
    notes = create_lead_from_wedding(_cleaned(hotels_json="[]", hotels_tbd="1")).notes
    assert notes.startswith("WEDDING — ")
    assert "Guests riding the shuttle: 105" in notes
    assert "!! Hotels NOT BOOKED" in notes
    assert "Legs:" not in notes


def test_the_office_is_told_the_details_are_waiting():
    lead = create_lead_from_wedding(_cleaned())
    note = Notification.objects.get(lead=lead)
    assert note.title == "New wedding request: Jane Rider"
    assert "The Oak Barn at Loyalty" in note.detail
    assert "movement" not in note.detail


def test_a_customer_updating_their_details_never_touches_the_trips_we_built():
    lead = create_lead_from_wedding(_cleaned())
    ReservationFactory(lead=lead)
    create_lead_from_wedding(_cleaned(guest_count="140"), lead=lead)
    lead.refresh_from_db()
    assert lead.intake_payload["guest_count"] == 140
    assert lead.reservations.count() == 1


def test_an_update_leaves_the_agents_notes_alone():
    lead = create_lead_from_wedding(_cleaned())
    Lead.objects.filter(pk=lead.pk).update(notes="Called Jane — wants a trolley.")
    lead.refresh_from_db()
    create_lead_from_wedding(_cleaned(guest_count="140"), lead=lead)
    lead.refresh_from_db()
    assert lead.notes == "Called Jane — wants a trolley."


# --- the page ----------------------------------------------------------------------


def test_the_flow_reviews_the_answers_instead_of_building_a_day(client):
    html = client.get(PLAN_URL).content.decode()
    assert "showStep('review')" in html
    assert "Build my day" not in html
    assert "legs_json" not in html
    assert 'name="notes"' in html


def test_the_review_step_reads_the_answers_back_as_cards(client):
    page = client.get(PLAN_URL).content.decode()
    html = page[page.index("showStep('review')") :]
    html = html[: html.index("</fieldset>")]
    # the day: date headline + a ceremony-to-last-call timeline, and its own Edit times
    assert 'x-text="reviewDay"' in html
    assert "Everyone out by" in html
    assert "Edit times" in html
    # riders: a headline total over one tile per group
    assert 'x-text="riderTotal"' in html
    assert "roughly" not in html
    # every card edits in place — no bare "Change" links left over from the list
    assert ">Change</button>" not in html


def test_a_submission_creates_a_trip_less_lead_and_redirects_to_thanks(client):
    response = client.post(PLAN_URL, _post())
    assert response.status_code == 302
    assert "/thanks" in response["Location"]
    assert Lead.objects.get().reservations.count() == 0


def test_the_thanks_page_confirms_the_details_and_who_builds_the_day(client):
    page = client.get(client.post(PLAN_URL, _post())["Location"]).content.decode()
    assert "one of our wedding professionals is building your day" in page.lower()
    assert "Guests riding the shuttle" in page
    assert "Lansdowne Resort" in page
    assert Lead.objects.get().quote_no in page


def test_the_confirmation_email_repeats_every_detail(client, mailoutbox, settings):
    settings.PUBLIC_BASE_URL = "https://example.test"
    client.post(PLAN_URL, _post())
    (mail,) = mailoutbox
    assert "building your day" in mail.body
    assert "Ceremony location" in mail.body
    assert "St. John's Church" in mail.body
    assert "/weddings/plan/" in mail.body


def test_a_resume_link_rehydrates_the_answers(client):
    lead = create_lead_from_wedding(_cleaned())
    html = client.get(f"{PLAN_URL}{make_wedding_token(lead)}/").content.decode()
    assert "Lansdowne Resort" in html
    assert "Bridesmaids get ready" in html


def test_a_lead_that_is_not_a_wedding_has_no_answers():
    assert wedding_answers(LeadFactory().intake_payload) == []
    assert wedding_answers({"event": "invitee.created"}) == []


def test_a_wedding_without_an_email_is_refused(client):
    response = client.post(PLAN_URL, _post(email="", phone="2024242600"))
    assert response.status_code == 200
    assert not Lead.objects.exists()
