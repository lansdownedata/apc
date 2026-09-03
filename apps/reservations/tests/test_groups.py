"""APC-14 — linked sets of identical trips (`Reservation.group_key`)."""

from datetime import date, time
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory, VehicleTypeFactory
from apps.reservations import groups
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation, Stop

pytestmark = pytest.mark.django_db


def _group_of(reservation) -> list[Reservation]:
    return list(Reservation.objects.filter(group_key=reservation.group_key).order_by("sort_order"))


# --- building a set -------------------------------------------------------------------


def test_set_group_size_links_the_source_and_its_clones_under_one_key():
    res = TransferReservationFactory()

    groups.set_group_size(res, 4)

    res.refresh_from_db()
    assert res.group_key is not None
    assert res.lead.reservations.count() == 4
    assert {r.group_key for r in res.lead.reservations.all()} == {res.group_key}


def test_set_group_size_of_one_leaves_the_reservation_ungrouped():
    res = TransferReservationFactory()

    groups.set_group_size(res, 1)

    res.refresh_from_db()
    assert res.group_key is None
    assert res.lead.reservations.count() == 1


def test_set_group_size_gives_every_member_a_distinct_sort_order():
    res = TransferReservationFactory()

    groups.set_group_size(res, 5)

    orders = [r.sort_order for r in _group_of(res)]
    assert len(set(orders)) == 5
    assert orders == sorted(orders)


def test_group_clones_carry_the_whole_stop_including_flight_info():
    """A set is only "identical trips" if the airport run copies across too."""
    iad = AirportFactory(iata="IAD", timezone="America/New_York")
    united = AirlineFactory(iata="UA")
    res = TransferReservationFactory(pickup_timezone="America/New_York")
    Stop.objects.filter(reservation=res).delete()
    Stop.objects.create(
        reservation=res,
        sequence=0,
        address="IAD",
        name="Dulles curbside",
        note="meet inside",
        scheduled_time=time(14, 30),
        latitude=Decimal("38.944500"),
        longitude=Decimal("-77.455800"),
        airport=iad,
        airline=united,
        flight_number="123",
        flight_direction=Stop.FlightDirection.ARRIVAL,
    )
    Stop.objects.create(reservation=res, sequence=1, address="Hotel", scheduled_time=time(15, 30))

    groups.set_group_size(res, 2)

    clone = res.lead.reservations.exclude(pk=res.pk).get()
    pickup = clone.stops.order_by("sequence").first()
    assert pickup.name == "Dulles curbside"
    assert pickup.note == "meet inside"
    assert pickup.scheduled_time == time(14, 30)
    assert pickup.airport_id == iad.pk
    assert pickup.airline_id == united.pk
    assert pickup.flight_number == "123"
    assert pickup.flight_direction == Stop.FlightDirection.ARRIVAL
    assert clone.pickup_timezone == "America/New_York"


def test_group_clones_start_with_no_la_link_and_deferred_revenue():
    res = TransferReservationFactory(
        la_reservation_id="LA-1",
        trip_status=Reservation.TripStatus.DISPATCHED,
        revenue_status=Reservation.RevenueStatus.RECOGNIZED,
        recognized_amount=500,
    )

    groups.set_group_size(res, 3)

    for clone in res.lead.reservations.exclude(pk=res.pk):
        assert clone.la_reservation_id == ""
        assert clone.trip_status == ""
        assert clone.revenue_status == Reservation.RevenueStatus.DEFERRED
        assert clone.recognized_amount == 0
        assert clone.recognized_at is None


def test_set_group_size_is_capped():
    res = TransferReservationFactory()

    groups.set_group_size(res, 9999)

    assert res.lead.reservations.count() == groups.DUPLICATE_MAX


def test_set_group_size_rolls_back_entirely_when_a_clone_fails(monkeypatch):
    res = TransferReservationFactory()
    calls = {"n": 0}
    real = groups.clone_reservation

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(groups, "clone_reservation", explode)

    with pytest.raises(RuntimeError):
        groups.set_group_size(res, 5)

    res.refresh_from_db()
    assert res.lead.reservations.count() == 1
    assert res.group_key is None  # the anchor was never left half-grouped


# --- resizing an existing set ---------------------------------------------------------


def test_growing_an_existing_group_keeps_the_same_key():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    key = res.group_key

    groups.set_group_size(res, 6)

    res.refresh_from_db()
    assert res.group_key == key
    assert len(_group_of(res)) == 6


def test_shrinking_a_group_deletes_from_the_end():
    res = TransferReservationFactory()
    groups.set_group_size(res, 5)
    res.refresh_from_db()
    keep = [r.pk for r in _group_of(res)[:2]]

    groups.set_group_size(res, 2)

    assert [r.pk for r in _group_of(res)] == keep


def test_shrinking_never_deletes_the_reservation_being_edited():
    res = TransferReservationFactory()
    groups.set_group_size(res, 4)
    res.refresh_from_db()
    anchor = _group_of(res)[-1]  # the last copy, first in line to be removed

    groups.set_group_size(anchor, 2)

    survivors = [r.pk for r in _group_of(res)]
    assert anchor.pk in survivors
    assert len(survivors) == 2


def test_shrinking_to_one_clears_the_key_off_the_survivor():
    res = TransferReservationFactory()
    groups.set_group_size(res, 4)
    res.refresh_from_db()

    groups.set_group_size(res, 1)

    res.refresh_from_db()
    assert res.group_key is None
    assert res.lead.reservations.count() == 1


def test_shrinking_releases_the_affiliate_offers_on_the_removed_trips(monkeypatch):
    """The row cascades away with the trip, so the release has to happen before it."""
    from apps.dispatch import services as dispatch_services

    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    doomed = _group_of(res)[-1]
    AssignmentFactory(reservation=doomed, status=Assignment.Status.OFFERED)
    released = []
    real = dispatch_services.release_trips

    def spy(reservations, *, note):
        withdrawn = real(reservations, note=note)
        released.extend((a.reservation_id, a.status) for a in withdrawn)
        return withdrawn

    monkeypatch.setattr(dispatch_services, "release_trips", spy)

    groups.set_group_size(res, 2)

    assert released == [(doomed.pk, Assignment.Status.WITHDRAWN)]
    assert not Assignment.objects.filter(reservation_id=doomed.pk).exists()


def test_resizing_only_touches_its_own_group():
    lead = LeadFactory()
    mine = TransferReservationFactory(lead=lead)
    theirs = TransferReservationFactory(lead=lead)
    groups.set_group_size(mine, 3)
    groups.set_group_size(theirs, 3)
    mine.refresh_from_db()

    groups.set_group_size(mine, 1)

    theirs.refresh_from_db()
    assert len(_group_of(theirs)) == 3
    assert lead.reservations.count() == 4


# --- apply to all in group ------------------------------------------------------------


def test_propagated_fields_are_pinned():
    """Drift guard: a new Reservation field must be a deliberate propagate-or-not call."""
    assert groups.propagated_fields() == {
        "trip_type",
        "service_type",
        "vehicle",
        "pickup_date",
        "pickup_time",
        "pickup_timezone",
        "dropoff_date",
        "dropoff_time",
        "passengers",
        "rate",
        "hours",
        "min_hours",
        "gratuity_pct",
        "gratuity_flat",
        "discount_pct",
        "discount_flat",
    }


def test_apply_to_group_copies_the_editor_fields_onto_the_siblings():
    coach = VehicleTypeFactory(name="56-Passenger Coach")
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    res = TransferReservationFactory()
    groups.set_group_size(res, 4)
    res.refresh_from_db()
    res.vehicle = coach
    res.service_type = wedding
    res.pickup_date = date(2026, 10, 3)
    res.pickup_time = time(16, 0)
    res.passengers = 52
    res.rate = Decimal("950.00")
    res.save()

    groups.apply_to_group(res)

    for sibling in Reservation.objects.filter(group_key=res.group_key).exclude(pk=res.pk):
        assert sibling.vehicle_id == coach.pk
        assert sibling.service_type_id == wedding.pk
        assert sibling.pickup_date == date(2026, 10, 3)
        assert sibling.pickup_time == time(16, 0)
        assert sibling.passengers == 52
        assert sibling.rate == Decimal("950.00")


def test_apply_to_group_leaves_each_copys_own_lifecycle_alone():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    sibling = Reservation.objects.filter(group_key=res.group_key).exclude(pk=res.pk).first()
    sibling.la_reservation_id = "LA-9"
    sibling.trip_status = Reservation.TripStatus.DISPATCHED
    sibling.revenue_status = Reservation.RevenueStatus.RECOGNIZED
    sibling.save()
    order = sibling.sort_order
    res.la_reservation_id = "LA-1"
    res.trip_status = Reservation.TripStatus.ON_THE_WAY
    res.save()

    groups.apply_to_group(res)

    sibling.refresh_from_db()
    assert sibling.la_reservation_id == "LA-9"
    assert sibling.trip_status == Reservation.TripStatus.DISPATCHED
    assert sibling.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    assert sibling.sort_order == order
    assert sibling.group_key == res.group_key


def test_apply_to_group_replaces_the_sibling_route():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, address="Church", name="Ceremony")
    Stop.objects.create(reservation=res, sequence=1, address="Vineyard", note="rear gate")
    Stop.objects.create(reservation=res, sequence=2, address="Hotel")

    groups.apply_to_group(res)

    for sibling in Reservation.objects.filter(group_key=res.group_key).exclude(pk=res.pk):
        stops = list(sibling.stops.order_by("sequence"))
        assert [s.address for s in stops] == ["Church", "Vineyard", "Hotel"]
        assert stops[0].name == "Ceremony"
        assert stops[1].note == "rear gate"


def test_apply_to_group_refreshes_the_sibling_pickup_timezone():
    lax = AirportFactory(iata="LAX", timezone="America/Los_Angeles")
    res = TransferReservationFactory()
    groups.set_group_size(res, 2)
    res.refresh_from_db()
    res.stops.all().delete()
    Stop.objects.create(reservation=res, sequence=0, address="LAX", airport=lax)
    Stop.objects.create(reservation=res, sequence=1, address="Santa Monica")
    res.refresh_pickup_timezone()

    groups.apply_to_group(res)

    sibling = Reservation.objects.filter(group_key=res.group_key).exclude(pk=res.pk).get()
    assert sibling.pickup_timezone == "America/Los_Angeles"


def test_apply_to_group_never_reaches_another_group():
    lead = LeadFactory()
    mine = TransferReservationFactory(lead=lead, passengers=2)
    theirs = TransferReservationFactory(lead=lead, passengers=7)
    groups.set_group_size(mine, 2)
    groups.set_group_size(theirs, 2)
    mine.refresh_from_db()
    mine.passengers = 40
    mine.save()

    groups.apply_to_group(mine)

    theirs.refresh_from_db()
    assert [r.passengers for r in _group_of(theirs)] == [7, 7]


def test_apply_to_group_on_an_ungrouped_reservation_does_nothing():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, passengers=3)
    other = TransferReservationFactory(lead=lead, passengers=9)

    assert groups.apply_to_group(res) == []

    other.refresh_from_db()
    assert other.passengers == 9


def test_apply_to_group_reads_the_source_route_once_not_once_per_sibling():
    """`Reservation.pickup`/`ordered_stops` are properties over `stops`, so a route read
    per sibling is the easy N+1 here. Cost must grow only by each sibling's own writes."""

    def cost(size: int) -> int:
        res = TransferReservationFactory()
        groups.set_group_size(res, size)
        res.refresh_from_db()
        with CaptureQueriesContext(connection) as ctx:
            groups.apply_to_group(res)
        return len(ctx)

    three, nine = cost(3), cost(9)
    assert (nine - three) % 6 == 0
    per_sibling = (nine - three) // 6
    assert per_sibling == 3  # its own update, one stop-delete, one bulk_create


def test_apply_to_group_returns_the_siblings_it_updated():
    """The caller needs the rows, not just a count — an LA-synced sibling has to be
    flagged stale the same way a hand-edited one is."""
    res = TransferReservationFactory()
    groups.set_group_size(res, 4)
    res.refresh_from_db()
    expected = {r.pk for r in _group_of(res)} - {res.pk}

    updated = groups.apply_to_group(res)

    assert {r.pk for r in updated} == expected


# --- collapsing a set into one quote line ---------------------------------------------


def test_as_lines_collapses_a_set_into_a_single_line():
    lead = LeadFactory()
    alone = TransferReservationFactory(lead=lead)
    setof3 = TransferReservationFactory(lead=lead)
    groups.set_group_size(setof3, 3)

    lines = groups.as_lines(lead.reservations.all())

    assert [line.size for line in lines] == [1, 3]
    assert [line.reservation.pk for line in lines] == [alone.pk, setof3.pk]
    assert lines[0].is_group is False
    assert lines[1].is_group is True


def test_a_line_totals_every_trip_in_its_set():
    res = TransferReservationFactory(rate=200, hours=1)
    groups.set_group_size(res, 4)

    line = groups.as_lines(res.lead.reservations.all())[0]

    assert line.total == res.line_total * 4


def test_as_lines_keeps_two_different_sets_apart():
    lead = LeadFactory()
    first = TransferReservationFactory(lead=lead)
    second = TransferReservationFactory(lead=lead)
    groups.set_group_size(first, 2)
    groups.set_group_size(second, 3)

    lines = groups.as_lines(lead.reservations.all())

    assert [line.size for line in lines] == [2, 3]


def test_a_lines_anchor_is_its_earliest_member():
    res = TransferReservationFactory()
    groups.set_group_size(res, 3)

    line = groups.as_lines(res.lead.reservations.all())[0]

    assert line.reservation.pk == res.pk
    assert [m.pk for m in line.members] == [r.pk for r in _group_of(res)]


# --- removing a whole set --------------------------------------------------------------


def test_delete_group_removes_every_member():
    lead = LeadFactory()
    doomed = TransferReservationFactory(lead=lead)
    keep = TransferReservationFactory(lead=lead)
    groups.set_group_size(doomed, 4)

    groups.delete_group(doomed)

    assert [r.pk for r in lead.reservations.all()] == [keep.pk]


def test_delete_group_releases_coverage_on_every_member(monkeypatch):
    from apps.dispatch import services as dispatch_services

    res = TransferReservationFactory()
    groups.set_group_size(res, 3)
    res.refresh_from_db()
    for member in _group_of(res):
        AssignmentFactory(reservation=member, status=Assignment.Status.OFFERED)
    released = []
    real = dispatch_services.release_trips
    monkeypatch.setattr(
        dispatch_services,
        "release_trips",
        lambda reservations, *, note: released.extend(real(reservations, note=note)) or released,
    )

    groups.delete_group(res)

    assert len(released) == 3
    assert not Assignment.objects.exists()


def test_delete_group_on_an_ungrouped_reservation_removes_just_that_trip():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead)
    other = TransferReservationFactory(lead=lead)

    groups.delete_group(res)

    assert [r.pk for r in lead.reservations.all()] == [other.pk]
