"""Server-side validation of a submitted wedding itinerary.

`legs_json` is the customer's *edited* plan, not the one we generated, so none of it is
trusted: the shape is checked here and the vehicle recommendation is re-derived.
"""

import json
from datetime import date, time, timedelta

import pytest
from django.utils import timezone

from apps.addresses.factories import VenueFactory
from apps.public.forms import WeddingRequestForm

pytestmark = pytest.mark.django_db


def _legs(n=2):
    legs = [
        {
            "id": "guests-in",
            "time": "15:00",
            "title": "Guests to the ceremony",
            "from": "Hampton Inn Leesburg",
            "from_sub": "Leesburg, VA",
            "to": "The Oak Barn at Loyalty",
            "to_sub": "Leesburg, VA",
            "pax": 105,
            "optional": False,
        },
        {
            "id": "final-out",
            "time": "23:00",
            "title": "Final return — last call",
            "from": "The Oak Barn at Loyalty",
            "from_sub": "",
            "to": "Hampton Inn Leesburg",
            "to_sub": "",
            "pax": 105,
            "optional": False,
        },
    ]
    while len(legs) < n:
        legs.append({**legs[0], "id": f"extra-{len(legs)}"})
    return legs[:n]


def _post(**over):
    data = {
        "name": "Jane Rider",
        "email": "jane@example.com",
        "phone": "",
        "wedding_date": (timezone.localdate() + timedelta(days=300)).isoformat(),
        "venue_name": "The Oak Barn at Loyalty",
        "same_site": "1",
        "groups": "guests",
        "guest_count": "105",
        "party_count": "12",
        "family_count": "8",
        "hotels_json": json.dumps([{"venue_id": None, "name": "Hampton Inn Leesburg"}]),
        "ceremony_time": "16:00",
        "end_time": "23:00",
        "legs_json": json.dumps(_legs()),
        "company": "",
    }
    data.update(over)
    return data


def test_a_complete_submission_validates():
    form = WeddingRequestForm(_post())
    assert form.is_valid(), form.errors


def test_the_honeypot_rejects_the_whole_form():
    form = WeddingRequestForm(_post(company="buy-cheap-coaches"))
    assert not form.is_valid()


def test_an_email_or_a_phone_is_required():
    assert not WeddingRequestForm(_post(email="", phone="")).is_valid()
    assert WeddingRequestForm(_post(email="", phone="2024242600")).is_valid()


def test_the_date_is_required():
    assert not WeddingRequestForm(_post(wedding_date="")).is_valid()


def test_a_venue_name_is_required():
    assert not WeddingRequestForm(_post(venue_name="")).is_valid()


def test_at_least_one_group_must_be_riding():
    form = WeddingRequestForm(_post(groups=""))
    assert not form.is_valid()
    assert "groups" in form.errors


def test_unknown_groups_are_dropped_not_fatal():
    form = WeddingRequestForm(_post(groups="guests,unicorns"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["groups"] == ["guests"]


def test_groups_keep_the_canonical_order():
    form = WeddingRequestForm(_post(groups="couple,guests,party"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["groups"] == ["guests", "party", "couple"]


# --- legs_json ---------------------------------------------------------------------


def test_at_least_one_leg_is_required():
    form = WeddingRequestForm(_post(legs_json=json.dumps([])))
    assert not form.is_valid()
    assert "legs_json" in form.errors


def test_thirteen_legs_are_rejected():
    form = WeddingRequestForm(_post(legs_json=json.dumps(_legs(13))))
    assert not form.is_valid()
    assert "legs_json" in form.errors


def test_twelve_legs_are_allowed():
    assert WeddingRequestForm(_post(legs_json=json.dumps(_legs(12)))).is_valid()


@pytest.mark.parametrize("pax", [0, 500, -3, "many"])
def test_impossible_passenger_counts_are_rejected(pax):
    legs = _legs()
    legs[0]["pax"] = pax
    assert not WeddingRequestForm(_post(legs_json=json.dumps(legs))).is_valid()


@pytest.mark.parametrize("field", ["time", "title", "from", "to"])
def test_every_leg_needs_its_core_fields(field):
    legs = _legs()
    legs[0][field] = ""
    assert not WeddingRequestForm(_post(legs_json=json.dumps(legs))).is_valid()


def test_an_unparseable_time_is_rejected():
    legs = _legs()
    legs[0]["time"] = "half past four"
    assert not WeddingRequestForm(_post(legs_json=json.dumps(legs))).is_valid()


def test_malformed_json_is_rejected_without_a_traceback():
    form = WeddingRequestForm(_post(legs_json="{not json"))
    assert not form.is_valid()
    assert "legs_json" in form.errors


def test_the_vehicle_recommendation_is_re_derived_server_side():
    """Whatever the client claims is ignored — the office quotes off our own rule."""
    legs = _legs()
    legs[0]["vehicle"] = "Unicorn carriage"
    form = WeddingRequestForm(_post(legs_json=json.dumps(legs)))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["legs"][0]["vehicle"] == "2 × 56-passenger coach"


def test_the_venues_cap_resizes_the_re_derived_recommendation():
    venue = VenueFactory(name="The Oak Barn at Loyalty", vehicle_cap=40)
    form = WeddingRequestForm(_post(venue_id=str(venue.pk)))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["legs"][0]["vehicle"] == "3 × 40-passenger coach"


def test_an_unknown_venue_id_is_rejected():
    assert not WeddingRequestForm(_post(venue_id="99999")).is_valid()


def test_legs_are_returned_in_time_order():
    legs = list(reversed(_legs()))
    form = WeddingRequestForm(_post(legs_json=json.dumps(legs)))
    assert form.is_valid(), form.errors
    assert [leg["time"] for leg in form.cleaned_data["legs"]] == [time(15, 0), time(23, 0)]


# --- the "not sure yet" path -------------------------------------------------------


def test_times_may_be_skipped_entirely():
    form = WeddingRequestForm(_post(ceremony_time="", end_time="", times_tbd="1"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["times_tbd"] is True


def test_hotels_may_be_skipped_entirely():
    form = WeddingRequestForm(_post(hotels_json="", hotels_tbd="1"))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["hotels"] == []
    assert form.cleaned_data["hotels_tbd"] is True


def test_a_hotel_naming_an_unknown_venue_id_is_rejected():
    hotels = json.dumps([{"venue_id": 99999, "name": "Ghost Inn"}])
    assert not WeddingRequestForm(_post(hotels_json=hotels)).is_valid()


def test_a_free_typed_hotel_needs_no_directory_row():
    hotels = json.dumps([{"venue_id": None, "name": "The Barn B&B"}])
    form = WeddingRequestForm(_post(hotels_json=hotels))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["hotels"][0].name == "The Barn B&B"


def test_a_past_date_is_still_accepted():
    """A typo'd year must not lose a lead — the flow warns, the office fixes it."""
    form = WeddingRequestForm(_post(wedding_date=date(2020, 6, 6).isoformat()))
    assert form.is_valid(), form.errors
