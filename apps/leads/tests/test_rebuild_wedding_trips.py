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


def _anchor(lead, leg_id):
    """The first trip on a leg. A leg maps to a SET now (APC-14) — 105 guests with no
    venue cap is two coaches — so a test about the leg's own fields asks the anchor."""
    return lead.reservations.filter(source_leg_id=leg_id).order_by("sort_order", "id").first()


@pytest.fixture
def lead(db):
    """A lead already carrying the two-leg plan from the public fixture."""
    lead = LeadFactory()
    rebuild_wedding_trips(lead, _clean())
    return lead


def test_a_first_build_covers_every_leg(lead):
    """Two legs of 105 guests, two 56-seat coaches each — the fixture has no venue cap."""
    assert lead.reservations.count() == 4
    assert sorted(r.source_leg_id for r in lead.reservations.all()) == [
        "final-out",
        "final-out",
        "guests-in",
        "guests-in",
    ]


def test_an_unchanged_leg_keeps_its_row_and_its_pricing(lead):
    res = _anchor(lead, "guests-in")
    res.rate = Decimal("150.00")
    res.la_reservation_id = "LA-4471"
    res.save()
    rebuild_wedding_trips(lead, _clean())
    res.refresh_from_db()
    assert res.rate == Decimal("150.00")
    assert res.la_reservation_id == "LA-4471"


def test_a_changed_time_updates_in_place(lead):
    res = _anchor(lead, "guests-in")
    legs = _legs()
    legs[0]["time"] = "14:30"
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))
    res.refresh_from_db()
    assert res.pickup_time.strftime("%H:%M") == "14:30"


def test_a_changed_headcount_updates_passengers(lead):
    legs = _legs()
    legs[0]["pax"] = 130
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))
    assert [r.passengers for r in _members(lead, "guests-in")] == [44, 43, 43]  # 130 → 3 coaches


def test_a_new_leg_creates_its_own_trips(lead):
    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs(3))))
    assert [r.source_leg_id for r in result.created] == ["extra-2", "extra-2"]
    assert lead.reservations.count() == 6


def test_a_removed_leg_is_reported_and_survives(lead):
    """Deleting it silently would take its payments, its LA reservation and its
    dispatch assignment with it."""
    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs()[:1])))
    assert [r.source_leg_id for r in result.orphans] == ["final-out", "final-out"]
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
    assert [r.sort_order for r in rows] == [0, 1, 2, 3, 4, 5]
    # Each leg's coaches sit together, and the legs stay in the order they were posted.
    # Time order, not posted order — `resolve_wedding` sorts the day by the clock, and
    # the third leg copies the first one's 15:00.
    assert [r.source_leg_id for r in rows] == [
        "guests-in",
        "guests-in",
        "extra-2",
        "extra-2",
        "final-out",
        "final-out",
    ]


def test_an_assigned_vehicle_snapshots_its_rate_card(lead):
    vehicle = VehicleTypeFactory(
        name="Wedding Coach", capacity=56, rate=Decimal("150.00"), transfer_min_hours=3
    )
    rebuild_wedding_trips(lead, _clean(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    res = _anchor(lead, "guests-in")
    assert res.vehicle == vehicle
    assert res.rate == Decimal("150.00")
    assert res.min_hours == 3


def test_a_leg_with_no_vehicle_posted_keeps_the_one_it_had(lead):
    """Rebuilding after a time change must not silently un-price the trip."""
    vehicle = VehicleTypeFactory(name="Wedding Coach", capacity=56, rate=Decimal("150.00"))
    rebuild_wedding_trips(lead, _clean(vehicles_json=json.dumps({"guests-in": vehicle.pk})))
    rebuild_wedding_trips(lead, _clean())
    res = _anchor(lead, "guests-in")
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
    original = {r.pk for r in lead.reservations.all()}
    assert len(original) == 4  # two legs, two coaches each

    legs = _legs()
    legs[0]["pax"] = 130
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    # Every original row survives — updated in place, never dropped and remade. The one
    # extra row is the third coach 130 guests now need, not a duplicate of the first two.
    assert original <= {r.pk for r in lead.reservations.all()}
    assert lead.reservations.count() == 5
    assert [r.passengers for r in _members(lead, "guests-in")] == [44, 43, 43]


# --- transfers by default, hourly when the office says so ---------------------------


def test_legs_are_transfers_unless_the_agent_says_otherwise(lead):
    """N transfers is the default shape — a wedding is a set of movements, not one
    open-ended charter, unless the office is running a continuous shuttle."""
    assert all(r.trip_type == "transfer" for r in lead.reservations.all())


def test_a_leg_can_be_billed_by_the_hour(lead):
    rebuild_wedding_trips(lead, _clean(trip_types_json=json.dumps({"guests-in": "hourly"})))
    assert _anchor(lead, "guests-in").trip_type == "hourly"
    assert _anchor(lead, "final-out").trip_type == "transfer"


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
    res = _anchor(lead, "guests-in")
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
    res = _anchor(lead, "guests-in")
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
    res = _anchor(lead, "guests-in")
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
    res = _anchor(lead, "guests-in")
    assert res.trip_type == "transfer"
    assert res.hours == 0
    assert res.dropoff_time is None


def test_a_rebuild_that_posts_no_trip_type_leaves_the_leg_alone(lead):
    """An agent changing a time must not silently flip an hourly shuttle to a transfer."""
    rebuild_wedding_trips(lead, _clean(trip_types_json=json.dumps({"guests-in": "hourly"})))
    rebuild_wedding_trips(lead, _clean())
    assert _anchor(lead, "guests-in").trip_type == "hourly"


# --- APC-14: a leg that needs several coaches builds a linked set -----------------------


def _members(lead, leg_id):
    return list(lead.reservations.filter(source_leg_id=leg_id).order_by("sort_order", "id"))


def test_a_leg_that_needs_two_coaches_builds_a_linked_pair(lead):
    """105 guests with no venue cap is two 56-seat coaches — the number the couple was
    already shown on the itinerary chip."""
    coaches = _members(lead, "guests-in")

    assert len(coaches) == 2
    assert coaches[0].group_key is not None
    assert coaches[0].group_key == coaches[1].group_key


def test_the_coaches_split_the_legs_guests(lead):
    assert [c.passengers for c in _members(lead, "guests-in")] == [53, 52]


def test_a_leg_a_single_vehicle_covers_stays_one_unlinked_trip(lead):
    legs = _legs()
    legs[0]["pax"] = 12
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    coaches = _members(lead, "guests-in")
    assert len(coaches) == 1
    assert coaches[0].group_key is None
    assert coaches[0].passengers == 12


def test_every_coach_carries_the_legs_route(lead):
    for coach in _members(lead, "guests-in"):
        assert [s.name for s in coach.ordered_stops] == [
            "Hampton Inn Leesburg",
            "The Oak Barn at Loyalty",
        ]


def test_rebuilding_an_unchanged_plan_keeps_every_coach_in_place(lead):
    """The whole point of reconciling: a second coach must not be dropped and remade,
    or it loses its price, its LA id and its affiliate."""
    before = _members(lead, "guests-in")
    before[1].rate = Decimal("415.00")
    before[1].la_reservation_id = "LA-8891"
    before[1].save()

    rebuild_wedding_trips(lead, _clean())

    after = _members(lead, "guests-in")
    assert [c.pk for c in after] == [c.pk for c in before]
    assert after[1].rate == Decimal("415.00")
    assert after[1].la_reservation_id == "LA-8891"


def test_a_bigger_headcount_grows_the_set(lead):
    legs = _legs()
    legs[0]["pax"] = 160
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    coaches = _members(lead, "guests-in")
    assert len(coaches) == 3
    assert [c.passengers for c in coaches] == [54, 53, 53]


def test_a_smaller_headcount_shrinks_the_set(lead):
    legs = _legs()
    legs[0]["pax"] = 40
    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    assert len(_members(lead, "guests-in")) == 1


def test_an_office_count_wins_over_the_derived_one(lead):
    """The agent knows the coach is already half-booked, or the couple asked for a spare."""
    rebuild_wedding_trips(lead, _clean(counts_json=json.dumps({"guests-in": 4})))

    coaches = _members(lead, "guests-in")
    assert len(coaches) == 4
    assert [c.passengers for c in coaches] == [27, 26, 26, 26]


def test_an_office_count_of_one_collapses_the_set(lead):
    rebuild_wedding_trips(lead, _clean(counts_json=json.dumps({"guests-in": 1})))

    coaches = _members(lead, "guests-in")
    assert len(coaches) == 1
    assert coaches[0].group_key is None
    assert coaches[0].passengers == 105


def test_a_junk_count_falls_back_to_the_derived_one(lead):
    rebuild_wedding_trips(lead, _clean(counts_json=json.dumps({"guests-in": "lots"})))

    assert len(_members(lead, "guests-in")) == 2


def test_every_coach_gets_the_legs_vehicle_and_rate_card(lead):
    coach = VehicleTypeFactory(name="56-Passenger Coach", rate=Decimal("330.00"))
    rebuild_wedding_trips(lead, _clean(vehicles_json=json.dumps({"guests-in": coach.pk})))

    for member in _members(lead, "guests-in"):
        assert member.vehicle_id == coach.pk
        assert member.rate == Decimal("330.00")


def test_removing_a_leg_orphans_every_coach_on_it(lead):
    doomed = {c.pk for c in _members(lead, "guests-in")}

    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs()[1:])))

    assert {r.pk for r in result.orphans} == doomed


def test_the_rebuild_reports_every_coach_it_created(lead):
    legs = _legs()
    legs.append({**legs[0], "id": "extra-out", "pax": 105})

    result = rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(legs)))

    assert len([r for r in result.created if r.source_leg_id == "extra-out"]) == 2


def test_a_rebuild_keeps_each_coach_in_its_place(lead):
    """Coach 1 must still be coach 1 afterwards. Its position decides its share of the
    guests and is what an affiliate already holding it was told — a set that reshuffles
    hands trip 1's headcount to trip 2.

    Adding a leg is what exposes it: the whole day is renumbered, so a set whose order
    is read off the `sort_order` being rewritten sees its own half-finished state.
    """
    before = [c.pk for c in _members(lead, "final-out")]

    rebuild_wedding_trips(lead, _clean(legs_json=json.dumps(_legs(3))))

    assert [c.pk for c in _members(lead, "final-out")] == before
