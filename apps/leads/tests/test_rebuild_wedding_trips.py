"""Rebuilding a wedding's trips reconciles — it never deletes and recreates.

An unchanged leg keeping its row is what keeps its pricing, its LimoAnywhere reservation
id and its dispatch assignment alive across an edit.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.forms import PortalWeddingForm
from apps.leads.services import rebuild_wedding_trips
from apps.public.tests.test_wedding_form import _legs, _post
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _portal(**over):
    data = _post(**over)
    for field in ("name", "email", "phone", "company"):
        data.pop(field, None)
    return data


def _clean(**over):
    form = PortalWeddingForm(_portal(**over))
    assert form.is_valid(), form.errors
    return form.cleaned_data


@pytest.fixture
def lead(db):
    """A lead already carrying the two-leg plan from the public fixture."""
    lead = LeadFactory()
    rebuild_wedding_trips(lead, _clean())
    return lead


def test_a_first_build_creates_one_reservation_per_leg(lead):
    assert lead.reservations.count() == 2
    assert sorted(r.source_leg_id for r in lead.reservations.all()) == ["final-out", "guests-in"]


def test_an_unchanged_leg_keeps_its_row_and_its_pricing(lead):
    res = lead.reservations.get(source_leg_id="guests-in")
    res.rate = Decimal("150.00")
    res.la_reservation_id = "LA-4471"
    res.save()
    rebuild_wedding_trips(lead, _clean())
    res.refresh_from_db()
    assert res.rate == Decimal("150.00")
    assert res.la_reservation_id == "LA-4471"


def test_a_changed_time_updates_in_place(lead):
    res = lead.reservations.get(source_leg_id="guests-in")
    legs = _legs()
    legs[0]["time"] = "14:30"
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))
    res.refresh_from_db()
    assert res.pickup_time.strftime("%H:%M") == "14:30"


def test_a_changed_headcount_updates_passengers(lead):
    legs = _legs()
    legs[0]["pax"] = 130
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))
    assert lead.reservations.get(source_leg_id="guests-in").passengers == 130


def test_a_new_leg_creates_a_new_reservation(lead):
    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs(3))))
    assert len(result.created) == 1
    assert lead.reservations.count() == 3


def test_a_removed_leg_is_reported_and_survives(lead):
    """Deleting it silently would take its payments, its LA reservation and its
    dispatch assignment with it."""
    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs()[:1])))
    assert [r.source_leg_id for r in result.orphans] == ["final-out"]
    assert lead.reservations.filter(source_leg_id="final-out").exists()


def test_a_hand_added_trip_is_never_touched_and_never_orphaned(lead):
    """The builder owns the trips it generated and nothing else."""
    manual = ReservationFactory(lead=lead, passengers=3, source_leg_id="")
    result = rebuild_wedding_trips(lead, _clean())
    manual.refresh_from_db()
    assert manual.passengers == 3
    assert manual not in result.orphans


def test_each_reservation_keeps_exactly_two_ordered_stops(lead):
    rebuild_wedding_trips(lead, _clean())
    for res in lead.reservations.exclude(source_leg_id=""):
        assert [s.sequence for s in res.stops.all()] == [0, 1]


def test_sort_order_follows_the_posted_leg_order(lead):
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs(3))))
    rows = lead.reservations.exclude(source_leg_id="").order_by("sort_order")
    assert [r.sort_order for r in rows] == [0, 1, 2]


def test_an_assigned_vehicle_snapshots_its_rate_card(lead):
    vehicle = VehicleTypeFactory(
        name="Wedding Coach", capacity=56, rate=Decimal("150.00"), transfer_min_hours=3
    )
    rebuild_wedding_trips(lead, _clean(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.vehicle == vehicle
    assert res.rate == Decimal("150.00")
    assert res.min_hours == 3


def test_a_leg_with_no_vehicle_posted_keeps_the_one_it_had(lead):
    """Rebuilding after a time change must not silently un-price the trip."""
    vehicle = VehicleTypeFactory(name="Wedding Coach", capacity=56, rate=Decimal("150.00"))
    rebuild_wedding_trips(lead, _clean(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    rebuild_wedding_trips(lead, _clean())
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.vehicle == vehicle
    assert res.rate == Decimal("150.00")


def test_every_reservation_is_a_wedding_transfer(lead):
    for res in lead.reservations.exclude(source_leg_id=""):
        assert res.trip_type == "transfer"
        assert res.service_type.name.lower().startswith("wedding")


def test_the_notes_and_payload_are_rewritten(lead):
    rebuild_wedding_trips(lead, _clean(hotels_json="", hotels_tbd="1"))
    lead.refresh_from_db()
    assert "!! Hotels NOT BOOKED" in lead.notes
    assert lead.intake_payload["hotels_tbd"] is True


def test_the_payload_carries_the_leads_own_contact(lead):
    """The portal form has no contact fields — the payload takes them from the lead."""
    rebuild_wedding_trips(lead, _clean())
    lead.refresh_from_db()
    assert lead.intake_payload["name"] == lead.contact.name


def test_a_wedding_inside_the_alert_window_flags_the_lead(lead):
    soon = (timezone.localdate() + timedelta(days=20)).isoformat()
    rebuild_wedding_trips(lead, _clean(wedding_date=soon))
    lead.refresh_from_db()
    assert lead.has_alert is True
