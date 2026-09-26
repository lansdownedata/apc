"""Server-side validation of a submitted wedding: the answers, and nothing derived from them."""

import json
from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.public.forms import WeddingRequestForm

pytestmark = pytest.mark.django_db


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


def test_an_email_is_required_and_a_phone_is_optional():
    assert not WeddingRequestForm(_post(email="", phone="2024242600")).is_valid()
    assert WeddingRequestForm(_post(phone="")).is_valid()


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


def test_an_unknown_venue_id_is_rejected():
    assert not WeddingRequestForm(_post(venue_id="99999")).is_valid()


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
