"""A wedding becomes one Lead holding the couple's answers. See test_wedding_details.py
for the details themselves; this file keeps the lead-level rules."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.addresses.factories import VenueFactory
from apps.leads.models import Lead
from apps.public.forms import WeddingRequestForm
from apps.public.services import create_lead_from_wedding

from .test_wedding_form import _post

pytestmark = pytest.mark.django_db


def _lead(**over) -> Lead:
    form = WeddingRequestForm(_post(**over))
    assert form.is_valid(), form.errors
    return create_lead_from_wedding(form.cleaned_data)


def test_the_lead_lands_new_on_the_website_channel():
    lead = _lead()
    assert lead.status == Lead.Status.NEW
    assert lead.channel == "website"
    assert lead.contact.name == "Jane Rider"


# --- the notes an agent quotes from ------------------------------------------------


# --- alerts ------------------------------------------------------------------------


def test_a_wedding_inside_45_days_raises_an_alert():
    soon = (timezone.localdate() + timedelta(days=30)).isoformat()
    assert _lead(wedding_date=soon).has_alert is True


def test_a_wedding_well_out_does_not():
    later = (timezone.localdate() + timedelta(days=200)).isoformat()
    assert _lead(wedding_date=later).has_alert is False


def test_a_date_in_the_past_still_raises_an_alert():
    past = (timezone.localdate() - timedelta(days=5)).isoformat()
    assert _lead(wedding_date=past).has_alert is True


def test_a_returning_email_takes_the_name_on_the_wedding_form(db):
    from apps.contacts.factories import ContactFactory

    jane = ContactFactory(name="Jane Doe", email="jane@example.com")
    lead = _lead(name="Priya Whitfield")
    assert lead.contact == jane
    assert lead.contact.name == "Priya Whitfield"
    assert lead.notes.startswith("WEDDING — ")


def test_a_confirmed_plan_carries_no_warning_line():
    assert "!!" not in _lead().notes


def test_the_notes_name_the_venues_cap():
    """An agent building the trips by hand has to know what fits up the drive."""
    venue = VenueFactory(name="The Oak Barn at Loyalty", vehicle_cap=40)
    assert "Venue vehicle cap: 40 passengers" in _lead(venue_id=str(venue.pk)).notes
