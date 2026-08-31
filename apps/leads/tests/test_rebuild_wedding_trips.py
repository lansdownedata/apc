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


def test_a_website_wedding_survives_the_offices_first_edit(db):
    """The end-to-end case the two flows meet on: a customer builds the day, the office
    reopens it and changes something. The trips must be updated, never duplicated."""
    from apps.public.forms import WeddingRequestForm
    from apps.public.services import create_lead_from_wedding

    public = WeddingRequestForm(_post())
    assert public.is_valid(), public.errors
    lead = create_lead_from_wedding(public.cleaned_data)
    original = {r.source_leg_id: r.pk for r in lead.reservations.all()}
    assert len(original) == 2

    legs = _legs()
    legs[0]["pax"] = 130
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    assert lead.reservations.count() == 2
    assert {r.source_leg_id: r.pk for r in lead.reservations.all()} == original
    assert lead.reservations.get(source_leg_id="guests-in").passengers == 130


# --- transfers by default, hourly when the office says so ---------------------------


def test_legs_are_transfers_unless_the_agent_says_otherwise(lead):
    """N transfers is the default shape — a wedding is a set of movements, not one
    open-ended charter, unless the office is running a continuous shuttle."""
    assert all(r.trip_type == "transfer" for r in lead.reservations.all())


def test_a_leg_can_be_billed_by_the_hour(lead):
    rebuild_wedding_trips(lead, _clean(trip_types_json=json.dumps({"guests-in": "hourly"})))
    assert lead.reservations.get(source_leg_id="guests-in").trip_type == "hourly"
    assert lead.reservations.get(source_leg_id="final-out").trip_type == "transfer"


def test_an_hourly_leg_takes_the_vehicles_hourly_minimum(lead):
    vehicle = VehicleTypeFactory(
        name="Shuttle Coach",
        capacity=56,
        rate=Decimal("150.00"),
        transfer_min_hours=3,
        hourly_min_hours=8,
    )
    rebuild_wedding_trips(
        lead,
        _clean(
            vehicles_json=json.dumps({"guests-in": vehicle.pk}),
            trip_types_json=json.dumps({"guests-in": "hourly"}),
        ),
    )
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.min_hours == 8
    assert res.subtotal == Decimal("1200.00")  # 150 x 8


def test_an_hours_override_replaces_the_minimum(lead):
    vehicle = VehicleTypeFactory(
        name="Shuttle Coach", capacity=56, rate=Decimal("150.00"), hourly_min_hours=8
    )
    rebuild_wedding_trips(
        lead,
        _clean(
            vehicles_json=json.dumps({"guests-in": vehicle.pk}),
            trip_types_json=json.dumps({"guests-in": "hourly"}),
            hours_json=json.dumps({"guests-in": "10"}),
        ),
    )
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.hours == 10
    assert res.subtotal == Decimal("1500.00")  # 150 x 10, the override replaces the minimum


def test_an_hourly_leg_derives_its_drop_off_from_the_billed_hours(lead):
    """Same rule as the reservation editor: an hourly trip's end is pickup + billed
    hours, so dispatch and the customer's itinerary both have an end time."""
    vehicle = VehicleTypeFactory(name="Shuttle Coach", capacity=56, hourly_min_hours=8)
    rebuild_wedding_trips(
        lead,
        _clean(
            vehicles_json=json.dumps({"guests-in": vehicle.pk}),
            trip_types_json=json.dumps({"guests-in": "hourly"}),
        ),
    )
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.pickup_time.strftime("%H:%M") == "15:00"
    assert res.dropoff_time.strftime("%H:%M") == "23:00"  # 3pm + 8h
    assert res.dropoff_date == res.pickup_date


def test_switching_a_leg_back_to_transfer_drops_the_stale_hours(lead):
    """Otherwise a transfer keeps billing the hours it had while it was hourly."""
    rebuild_wedding_trips(
        lead,
        _clean(
            trip_types_json=json.dumps({"guests-in": "hourly"}),
            hours_json=json.dumps({"guests-in": "10"}),
        ),
    )
    rebuild_wedding_trips(lead, _clean(trip_types_json=json.dumps({"guests-in": "transfer"})))
    res = lead.reservations.get(source_leg_id="guests-in")
    assert res.trip_type == "transfer"
    assert res.hours == 0
    assert res.dropoff_time is None


def test_a_rebuild_that_posts_no_trip_type_leaves_the_leg_alone(lead):
    """An agent changing a time must not silently flip an hourly shuttle to a transfer."""
    rebuild_wedding_trips(lead, _clean(trip_types_json=json.dumps({"guests-in": "hourly"})))
    rebuild_wedding_trips(lead, _clean())
    assert lead.reservations.get(source_leg_id="guests-in").trip_type == "hourly"
