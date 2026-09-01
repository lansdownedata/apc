"""A wedding fans out into one Lead holding one Reservation per confirmed movement.

That is not a special case — it is `Lead -> many Reservation` used properly, the same
shape `create_lead_from_booking` already builds.
"""

import json
from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.addresses.factories import VenueFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.public.forms import WeddingRequestForm
from apps.public.services import create_lead_from_wedding
from apps.reservations.models import Reservation

from .test_wedding_form import _legs, _post

pytestmark = pytest.mark.django_db


def _lead(**over) -> Lead:
    form = WeddingRequestForm(_post(**over))
    assert form.is_valid(), form.errors
    return create_lead_from_wedding(form.cleaned_data)


def test_one_lead_holds_one_reservation_per_leg():
    lead = _lead(legs_json=json.dumps(_legs(4)))
    assert Lead.objects.count() == 1
    assert lead.reservations.count() == 4
    assert [r.sort_order for r in lead.reservations.all()] == [0, 1, 2, 3]


def test_the_lead_lands_new_on_the_website_channel():
    lead = _lead()
    assert lead.status == Lead.Status.NEW
    assert lead.channel == "website"
    assert lead.contact.name == "Jane Rider"


def test_every_reservation_is_a_wedding_transfer_on_the_wedding_date():
    lead = _lead()
    wedding_date = timezone.localdate() + timedelta(days=300)
    for res in lead.reservations.all():
        assert res.trip_type == Reservation.TripType.TRANSFER
        assert res.service_type is not None
        assert res.service_type.name.lower().startswith("wedding")
        assert res.pickup_date == wedding_date


def test_the_wedding_service_type_reuses_the_settings_catalog_entry():
    """One catalog for the website and the office — never a second wedding row."""
    from apps.leads.models import ServiceType

    _lead()
    _lead(email="other@example.com")
    assert ServiceType.objects.filter(name__istartswith="Wedding").count() == 1


def test_each_reservation_carries_its_own_time_and_headcount():
    lead = _lead()
    first, last = lead.reservations.all()
    assert (first.pickup_time, first.passengers) == (time(15, 0), 105)
    assert (last.pickup_time, last.passengers) == (time(23, 0), 105)


def test_each_reservation_has_exactly_two_ordered_stops():
    lead = _lead()
    for res in lead.reservations.all():
        stops = list(res.stops.all())
        assert [s.sequence for s in stops] == [0, 1]
        assert stops[0].name and stops[1].name


def test_a_directory_venue_puts_its_address_on_the_stop():
    venue = VenueFactory(
        name="The Oak Barn at Loyalty",
        address="14572 Loyalty Rd",
        city="Leesburg",
        state="VA",
        latitude="39.115000",
        longitude="-77.564000",
    )
    lead = _lead(venue_id=str(venue.pk))
    stop = lead.reservations.first().stops.get(sequence=1)
    assert stop.name == "The Oak Barn at Loyalty"
    assert "14572 Loyalty Rd" in stop.address
    assert stop.latitude is not None


def test_no_vehicle_is_assigned_programmatically():
    """Assigning one would snapshot a rate card off a guess — a human picks it."""
    assert all(r.vehicle_id is None for r in _lead().reservations.all())


def test_the_office_gets_one_notification_naming_the_movement_count():
    lead = _lead(legs_json=json.dumps(_legs(3)))
    note = Notification.objects.get(kind=Notification.Kind.NEW_LEAD)
    assert note.lead_id == lead.pk
    assert "Jane Rider" in note.title
    assert "3 movements" in note.detail


# --- the notes an agent quotes from ------------------------------------------------


def test_the_notes_lead_with_the_wedding_and_its_venue():
    notes = _lead().notes
    assert notes.startswith("WEDDING — ")
    assert "The Oak Barn at Loyalty" in notes
    assert "Riding: Our guests" in notes


def test_the_notes_name_the_venues_cap():
    venue = VenueFactory(name="The Oak Barn at Loyalty", vehicle_cap=40)
    assert "vehicle cap 40 pax" in _lead(venue_id=str(venue.pk)).notes


def test_the_notes_list_every_movement_with_its_recommendation():
    notes = _lead().notes
    assert "Legs: 3:00 PM Guests to the ceremony (105p" in notes
    assert "2 × 56-passenger coach" in notes


def test_a_separate_ceremony_site_gets_its_own_line():
    notes = _lead(same_site="", ceremony_venue_name="St. Katharine Drexel Church").notes
    assert "Ceremony: St. Katharine Drexel Church" in notes


def test_a_customer_added_early_return_is_called_out_for_the_office():
    legs = _legs()
    legs.append({**legs[0], "id": "early-out", "title": "Early return run", "time": "23:00"})
    notes = _lead(legs_json=json.dumps(legs)).notes
    assert "Customer added an early return run — confirm its pickup time." in notes


def test_no_early_return_line_when_the_customer_did_not_add_one():
    assert "early return run" not in _lead().notes.lower()


def test_estimated_times_are_flagged_so_nobody_quotes_a_guess():
    notes = _lead(ceremony_time="", end_time="", times_tbd="1").notes
    assert "!! Times ESTIMATED" in notes


def test_unbooked_hotels_are_flagged_too():
    notes = _lead(hotels_json="", hotels_tbd="1").notes
    assert "!! Hotels NOT BOOKED" in notes
    assert "Hotels: not booked yet" in notes


def test_a_confirmed_plan_carries_no_warning_line():
    assert "!!" not in _lead().notes


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


def test_each_reservation_records_the_leg_it_came_from():
    """Without this the office's first edit duplicates the whole day: the portal's
    rebuild matches on source_leg_id, and a blank one reads as a hand-added trip."""
    lead = _lead()
    assert sorted(r.source_leg_id for r in lead.reservations.all()) == ["final-out", "guests-in"]


def test_a_wedding_records_the_name_on_the_form_when_it_differs(db):
    """Same reason as the booking form: the office needs to see who actually filled it."""
    from apps.contacts.factories import ContactFactory

    ContactFactory(name="James Bond", email="jane@example.com")
    lead = _lead(name="Priya Whitfield")
    assert lead.contact.name == "James Bond"
    assert lead.notes.startswith("Submitted as: Priya Whitfield")
    assert "WEDDING — " in lead.notes
