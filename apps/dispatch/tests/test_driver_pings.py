"""Driver link location pings: gating, throttle, validation and the 90-day purge."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from apps.core import cron
from apps.dispatch import driver_link
from apps.dispatch.driver_link import BadCoordinates, apply_status, purge_driver_pings, record_ping
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DriverPing
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _live(status=S.ON_THE_WAY, **kw):
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 11, 12),
        pickup_time=time(9, 0),
    )
    assignment = AssignmentFactory(
        reservation=trip, status=kw.pop("assignment_status", Assignment.Status.CONFIRMED)
    )
    set_trip_status(trip, status)
    return assignment


def _ping(assignment, **kw):
    kw.setdefault("now", NOW)
    return record_ping(assignment.pk, lat=38.85, lng=-77.04, **kw)


@pytest.mark.parametrize("status", driver_link.PING_STATUSES)
def test_ping_accepted_while_under_way(status):
    a = _live(status)
    ping = _ping(a, accuracy=12.5, speed=3.0, heading=90)
    assert ping is not None
    assert ping.kind == DriverPing.Kind.TRACK
    assert ping.reservation_id == a.reservation_id
    assert ping.accuracy_m == 12.5
    assert ping.created_at == NOW


@pytest.mark.parametrize("status", [S.DISPATCHED, S.DONE])
def test_ping_refused_at_dispatched_and_done(status):
    a = _live(status)
    assert _ping(a) is None
    assert DriverPing.objects.count() == 0


def test_ping_refused_on_a_dead_link():
    a = _live(assignment_status=Assignment.Status.WITHDRAWN)
    assert _ping(a) is None
    assert DriverPing.objects.count() == 0


def test_ping_throttled_under_fifteen_seconds():
    a = _live()
    assert _ping(a) is not None
    assert _ping(a, now=NOW + timedelta(seconds=10)) is None
    assert _ping(a, now=NOW + timedelta(seconds=16)) is not None
    assert DriverPing.objects.count() == 2


def test_bad_coordinates_raise():
    a = _live()
    with pytest.raises(BadCoordinates):
        record_ping(a.pk, lat=91, lng=0, now=NOW)
    with pytest.raises(BadCoordinates):
        record_ping(a.pk, lat=0, lng=-181, now=NOW)
    with pytest.raises(BadCoordinates):
        record_ping(a.pk, lat=0, lng=0, accuracy=-1, now=NOW)
    with pytest.raises(BadCoordinates):
        record_ping(a.pk, lat="junk", lng=0, now=NOW)
    with pytest.raises(BadCoordinates):
        record_ping(a.pk, lat=float("nan"), lng=0, now=NOW)
    assert DriverPing.objects.count() == 0


def test_status_tap_with_junk_accuracy_still_advances():
    a = _live(S.DISPATCHED)
    apply_status(
        a.pk,
        to=S.ON_THE_WAY,
        from_=S.DISPATCHED,
        coords={"lat": 38.85, "lng": -77.04, "accuracy": "junk"},
        now=NOW,
    )
    a.reservation.refresh_from_db()
    assert a.reservation.trip_status == S.ON_THE_WAY
    ping = DriverPing.objects.get()
    assert ping.kind == DriverPing.Kind.STATUS
    assert ping.accuracy_m is None


def test_purge_deletes_only_older_than_ninety_days():
    a = _live()
    old = DriverPing.objects.create(
        assignment=a,
        reservation=a.reservation,
        kind=DriverPing.Kind.TRACK,
        latitude=1,
        longitude=1,
        created_at=NOW - timedelta(days=91),
    )
    fresh = DriverPing.objects.create(
        assignment=a,
        reservation=a.reservation,
        kind=DriverPing.Kind.TRACK,
        latitude=1,
        longitude=1,
        created_at=NOW - timedelta(days=89),
    )
    assert purge_driver_pings(now=NOW) == 1
    assert not DriverPing.objects.filter(pk=old.pk).exists()
    assert DriverPing.objects.filter(pk=fresh.pk).exists()


def test_purge_is_a_cron_job():
    assert "purge-driver-pings" in cron.JOBS
