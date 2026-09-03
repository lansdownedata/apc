"""Leg generation — the pure function every wedding quote comes from.

Tested hardest of anything in the flow: the customer never types a pickup time, so
every one of these offsets is a promise the office has to keep.
"""

from datetime import time

import pytest

from apps.public.wedding import (
    DEFAULT_CEREMONY_TIME,
    DEFAULT_END_TIME,
    VEHICLE_CERTAIN_MAX,
    Site,
    WeddingPlan,
    early_return_leg,
    generate_legs,
    hotel_label,
    vehicle_for,
    vehicle_is_certain,
)


def plan(**over) -> WeddingPlan:
    data = {
        "wedding_date": None,
        "venue": Site(name="The Oak Barn at Loyalty", sub="Leesburg, VA"),
        "ceremony": None,
        "same_site": True,
        "groups": ["guests"],
        "guest_count": 100,
        "party_count": 12,
        "family_count": 8,
        "hotels": [],
        "hotels_tbd": False,
        "ceremony_time": time(16, 0),
        "end_time": time(23, 0),
        "times_tbd": False,
    }
    data.update(over)
    return WeddingPlan(**data)


def ids(legs):
    return [leg.id for leg in legs]


def at(legs, leg_id):
    return next(leg for leg in legs if leg.id == leg_id)


# --- which legs get generated ------------------------------------------------------


def test_guests_only_makes_two_runs():
    legs = generate_legs(plan(groups=["guests"]))
    assert ids(legs) == ["guests-in", "final-out"]


def test_no_early_return_run_is_generated_by_default():
    """APC-7 / feedback A3.2 — return timing is the customer's or the office's to set."""
    assert "early-out" not in ids(generate_legs(plan(groups=["guests"])))
    assert not any(leg.optional for leg in generate_legs(plan(groups=["guests"])))


def test_guests_and_party_make_three():
    legs = generate_legs(plan(groups=["guests", "party"]))
    assert len(legs) == 3
    assert "party-in" in ids(legs)


def test_family_adds_its_own_inbound_run():
    legs = generate_legs(plan(groups=["guests", "family"]))
    assert ids(legs) == ["family-in", "guests-in", "final-out"]


def test_couple_only_is_a_single_two_passenger_exit():
    legs = generate_legs(plan(groups=["couple"]))
    assert ids(legs) == ["exit"]
    assert legs[0].passengers == 2


def test_two_locations_add_the_ceremony_to_reception_hop():
    legs = generate_legs(
        plan(
            same_site=False, ceremony=Site(name="St. Katharine Drexel Church", sub="Haymarket, VA")
        )
    )
    assert "hop" in ids(legs)
    assert at(legs, "hop").time == time(16, 45)


def test_one_site_means_no_hop():
    assert "hop" not in ids(generate_legs(plan()))


def test_legs_come_back_in_time_order():
    legs = generate_legs(
        plan(
            groups=["guests", "party", "family", "couple"],
            same_site=False,
            ceremony=Site(name="St. John the Apostle"),
        )
    )
    assert [leg.time for leg in legs] == sorted(leg.time for leg in legs)


# --- the offsets themselves --------------------------------------------------------


def test_offsets_are_exact():
    legs = generate_legs(
        plan(
            groups=["guests", "party", "family", "couple"],
            same_site=False,
            ceremony=Site(name="St. John the Apostle"),
            ceremony_time=time(16, 0),
            end_time=time(23, 0),
        )
    )
    assert at(legs, "party-in").time == time(14, 45)  # ceremony − 75
    assert at(legs, "family-in").time == time(14, 50)  # ceremony − 70
    assert at(legs, "guests-in").time == time(15, 0)  # ceremony − 60
    assert at(legs, "hop").time == time(16, 45)  # ceremony + 45
    assert at(legs, "final-out").time == time(23, 0)  # end
    assert at(legs, "exit").time == time(23, 0)  # end


def test_offsets_wrap_around_midnight_rather_than_underflowing():
    legs = generate_legs(plan(groups=["party"], ceremony_time=time(0, 30)))
    assert at(legs, "party-in").time == time(23, 15)


# --- passenger counts --------------------------------------------------------------


def test_each_run_carries_its_own_group():
    legs = generate_legs(
        plan(groups=["guests", "party", "family"], guest_count=105, party_count=12, family_count=8)
    )
    assert at(legs, "guests-in").passengers == 105
    assert at(legs, "party-in").passengers == 12
    assert at(legs, "family-in").passengers == 8
    assert at(legs, "final-out").passengers == 105


def test_the_hop_carries_everyone_who_is_riding():
    legs = generate_legs(
        plan(
            groups=["guests", "party", "family"],
            guest_count=105,
            party_count=12,
            family_count=8,
            same_site=False,
            ceremony=Site(name="St. John the Apostle"),
        )
    )
    assert at(legs, "hop").passengers == 125


def test_the_early_return_leg_is_forty_percent_of_guests():
    assert early_return_leg(plan(guest_count=105)).passengers == 42


def test_the_early_return_leg_never_drops_below_twelve():
    assert early_return_leg(plan(guest_count=10)).passengers == 12


def test_the_early_return_leg_is_optional_and_carries_no_early_time():
    """Opt-in only (APC-7): it lands on the end time, for the couple to pull earlier."""
    leg = early_return_leg(plan(guest_count=105, end_time=time(23, 0)))
    assert leg.optional
    assert leg.why
    assert leg.id == "early-out"
    assert leg.time == time(23, 0)  # the end — never a suggested early time
    assert leg.vehicle


# --- vehicle recommendation --------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected",
    [
        (1, "Executive SUV"),
        (6, "Executive SUV"),
        (7, "Sprinter van"),
        (14, "Sprinter van"),
        (15, "Mini bus"),
        (24, "Mini bus"),
        (25, "Executive mini coach"),
        (38, "Executive mini coach"),
        (39, "Motorcoach"),
        (56, "Motorcoach"),
        (57, "2 × 56-passenger coach"),
        (112, "2 × 56-passenger coach"),
        (113, "3 × 56-passenger coach"),
    ],
)
def test_vehicle_boundaries_with_no_venue_cap(count, expected):
    assert vehicle_for(count, None) == expected


def test_a_venue_cap_resizes_the_run():
    assert vehicle_for(105, 40) == "3 × 40-passenger coach"


def test_a_cap_above_our_biggest_coach_is_ignored():
    assert vehicle_for(105, 300) == "2 × 56-passenger coach"


def test_a_cap_does_not_split_a_run_that_already_fits_a_smaller_vehicle():
    """Under 39 the class, not the coach count, is the recommendation."""
    assert vehicle_for(24, 40) == "Mini bus"


def test_generated_legs_carry_the_venues_cap():
    legs = generate_legs(
        plan(venue=Site(name="The Oak Barn at Loyalty", vehicle_cap=40), guest_count=105)
    )
    assert at(legs, "guests-in").vehicle == "3 × 40-passenger coach"


# --- when we can show a specific vehicle to the customer (APC-6 / feedback A3.1) ---


def test_a_small_group_is_certain_without_any_venue_cap():
    """At or below the coach threshold the class is unambiguous — no venue bans it."""
    assert vehicle_is_certain(VEHICLE_CERTAIN_MAX, None)
    assert vehicle_is_certain(6, None)


def test_a_coach_sized_group_is_uncertain_without_a_venue_cap():
    """Above the threshold the answer is 'how many coaches' — only the cap settles that."""
    assert not vehicle_is_certain(VEHICLE_CERTAIN_MAX + 1, None)
    assert not vehicle_is_certain(220, None)


def test_a_venue_cap_on_file_makes_any_size_certain():
    assert vehicle_is_certain(220, 40)
    assert vehicle_is_certain(VEHICLE_CERTAIN_MAX + 1, 56)


def test_the_certainty_threshold_is_the_vehicle_for_coach_boundary():
    """The cut-off must track `vehicle_for`: the last count that names one class."""
    assert vehicle_for(VEHICLE_CERTAIN_MAX, None) == "Executive mini coach"
    assert vehicle_for(VEHICLE_CERTAIN_MAX + 1, None) == "Motorcoach"


# --- "not sure yet" ----------------------------------------------------------------


def test_times_tbd_falls_back_to_the_median_day():
    p = plan(times_tbd=True, ceremony_time=None, end_time=None)
    legs = generate_legs(p)
    assert DEFAULT_CEREMONY_TIME == time(16, 0)
    assert DEFAULT_END_TIME == time(23, 0)
    assert at(legs, "guests-in").time == time(15, 0)
    assert at(legs, "final-out").time == time(23, 0)


def test_times_tbd_flags_every_leg_as_estimated():
    assert all(leg.estimated for leg in generate_legs(plan(times_tbd=True)))


def test_confirmed_times_are_not_flagged_as_estimated():
    assert not any(leg.estimated for leg in generate_legs(plan()))


def test_hotels_tbd_still_produces_a_full_itinerary():
    """43% of inquiries are six months out. This path must complete, not block."""
    legs = generate_legs(plan(hotels=[], hotels_tbd=True, times_tbd=True))
    assert len(legs) == 2
    assert at(legs, "guests-in").origin.name == "Guest hotels (to be confirmed)"


# --- hotel labelling ---------------------------------------------------------------


def test_one_hotel_reads_as_itself():
    assert hotel_label([Site(name="Hampton Inn Leesburg", city="Leesburg")], False) == (
        "Hampton Inn Leesburg"
    )


def test_several_hotels_collapse_to_a_counted_label():
    hotels = [
        Site(name="Hampton Inn Leesburg", city="Leesburg"),
        Site(name="Homewood Suites Leesburg", city="Leesburg"),
    ]
    assert hotel_label(hotels, False) == "2 hotels — Hampton Inn, Homewood Suites"


def test_no_hotels_reads_as_to_be_confirmed():
    assert hotel_label([], True) == "Guest hotels (to be confirmed)"
    assert hotel_label([], False) == "Guest hotels (to be confirmed)"


# --- APC-14: how many vehicles a leg actually needs -------------------------------------


def test_vehicle_runs_is_one_for_anything_a_single_class_covers():
    from apps.public.wedding import vehicle_runs

    assert vehicle_runs(1, None) == 1
    assert vehicle_runs(24, None) == 1
    assert vehicle_runs(VEHICLE_CERTAIN_MAX, None) == 1


def test_vehicle_runs_divides_the_group_by_the_venues_cap():
    from apps.public.wedding import vehicle_runs

    assert vehicle_runs(105, 40) == 3
    assert vehicle_runs(80, 40) == 2


def test_vehicle_runs_never_seats_more_than_our_largest_coach():
    from apps.public.wedding import vehicle_runs

    assert vehicle_runs(105, 300) == 2  # a generous venue cap can't raise MAX_COACH_SEATS
    assert vehicle_runs(105, None) == 2


def test_vehicle_for_names_the_count_vehicle_runs_computes():
    """The chip and the trips it generates must never disagree about how many coaches."""
    from apps.public.wedding import vehicle_runs

    for count, cap in ((105, 40), (105, None), (200, 56), (39, None)):
        runs = vehicle_runs(count, cap)
        label = vehicle_for(count, cap)
        assert (f"{runs} × " in label) if runs > 1 else ("×" not in label)


def test_split_passengers_divides_a_group_evenly():
    from apps.public.wedding import split_passengers

    assert split_passengers(150, 3) == [50, 50, 50]
    assert split_passengers(8, 1) == [8]


def test_split_passengers_puts_the_remainder_on_the_earliest_coaches():
    from apps.public.wedding import split_passengers

    assert split_passengers(105, 2) == [53, 52]
    assert split_passengers(100, 3) == [34, 33, 33]


def test_split_passengers_always_seats_everyone():
    from apps.public.wedding import split_passengers

    for total in (1, 7, 39, 105, 400):
        for runs in (1, 2, 3, 7):
            share = split_passengers(total, runs)
            assert len(share) == runs
            assert sum(share) == total
            assert min(share) >= 1 or total < runs
