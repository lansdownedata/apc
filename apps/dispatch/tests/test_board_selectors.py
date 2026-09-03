"""APC-24 — `board_trips` over a date range, narrowed by vehicle / customer / group."""

from datetime import date, time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.contacts.factories import ContactFactory
from apps.dispatch import selectors, services
from apps.dispatch.board_filters import BoardFilters
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.reservations import groups
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

MON = date(2026, 9, 14)


def _f(**over) -> BoardFilters:
    base = dict(view="range", start=MON, end=MON + timedelta(days=6), anchor=MON)
    base.update(over)
    return BoardFilters(**base)


def _trip(day, **kw):
    lead = kw.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    kw.setdefault("pickup_time", time(9, 0))
    return ReservationFactory(lead=lead, pickup_date=day, **kw)


def test_board_trips_returns_every_booked_trip_in_the_window():
    _trip(MON)
    _trip(MON + timedelta(days=3))
    _trip(MON + timedelta(days=8))  # outside the week

    trips = selectors.board_trips(_f())

    assert {t.pickup_date for t in trips} == {MON, MON + timedelta(days=3)}


def test_board_trips_are_ordered_by_date_then_time():
    late = _trip(MON + timedelta(days=1), pickup_time=time(15, 0))
    early = _trip(MON, pickup_time=time(6, 0))
    mid = _trip(MON, pickup_time=time(11, 0))

    trips = selectors.board_trips(_f())

    assert [t.pk for t in trips] == [early.pk, mid.pk, late.pk]


def test_board_trips_filters_by_vehicle_type():
    sedan = VehicleTypeFactory(name="Sedan")
    coach = VehicleTypeFactory(name="56-Passenger Coach")
    keep = _trip(MON, vehicle=sedan)
    _trip(MON, vehicle=coach)

    trips = selectors.board_trips(_f(vehicle_type_id=sedan.pk))

    assert [t.pk for t in trips] == [keep.pk]


def test_board_trips_filters_by_customer():
    alice = ContactFactory()
    keep = _trip(MON, lead=LeadFactory(status=Lead.Status.BOOKED, contact=alice))
    _trip(MON)

    trips = selectors.board_trips(_f(contact_id=alice.pk))

    assert [t.pk for t in trips] == [keep.pk]


def test_board_trips_filters_by_linked_set():
    anchor = _trip(MON)
    groups.set_group_size(anchor, 3)
    _trip(MON)  # an unrelated trip

    key = str(anchor.group_key)
    trips = selectors.board_trips(_f(group_key=key))

    assert {str(t.group_key) for t in trips} == {key}
    assert len(trips) == 3


def test_board_trips_still_decorates_route_ends_and_coverage():
    trip = _trip(MON)
    services.assign_direct(trip, VendorFactory(), payout=1)

    [got] = selectors.board_trips(_f())

    assert got.pickup_stop is not None
    assert got.coverage == selectors.COVERAGE_CONFIRMED


def test_board_trips_query_count_is_flat_over_a_week(django_assert_max_num_queries):
    for offset in range(7):
        for hour in (8, 12, 16):
            t = _trip(MON + timedelta(days=offset), pickup_time=time(hour, 0))
            services.assign_direct(t, VendorFactory(), payout=1)

    with django_assert_max_num_queries(15):
        list(selectors.board_trips(_f()))


def test_day_groups_splits_a_multi_day_result_by_date():
    _trip(MON)
    _trip(MON)
    _trip(MON + timedelta(days=2))

    trips = selectors.board_trips(_f())
    grouped = selectors.day_groups(trips)

    assert [d for d, _, _ in grouped] == [MON, MON + timedelta(days=2)]
    assert [len(ts) for _, ts, _ in grouped] == [2, 1]
    assert grouped[0][2]["uncovered"] == 2


def test_day_groups_reuses_the_trip_objects_no_extra_queries():
    _trip(MON)
    _trip(MON + timedelta(days=1))
    trips = selectors.board_trips(_f())

    with CaptureQueriesContext(connection) as ctx:
        selectors.day_groups(trips)

    assert len(ctx) == 0
