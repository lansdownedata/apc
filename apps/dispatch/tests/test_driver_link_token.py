"""Driver link: the signed per-assignment token and when a link is live."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.signing import BadSignature

from apps.dispatch import driver_link
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.dispatch.services import assign_direct, send_back, withdraw
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.acknowledgements import make_driver_link_token, read_driver_link_token
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.reservations.services import record_cancellation, set_trip_status

pytestmark = pytest.mark.django_db

DONE = Reservation.TripStatus.DONE
NOON = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _confirmed(**trip_kwargs):
    trip_kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    trip = ReservationFactory(**trip_kwargs)
    return AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)


def _status_at(trip, status, moment):
    with patch("django.utils.timezone.now", return_value=moment):
        set_trip_status(trip, status)


def test_token_round_trips():
    a = _confirmed()

    assert read_driver_link_token(make_driver_link_token(a)) == a


def test_forged_token_raises_bad_signature():
    a = _confirmed()

    with pytest.raises(BadSignature):
        read_driver_link_token(make_driver_link_token(a) + "x")


def test_live_on_confirmed_coverage():
    assert driver_link.is_live(_confirmed(), now=NOON)


def test_offered_assignment_is_not_live():
    a = AssignmentFactory(status=Assignment.Status.OFFERED)

    assert not driver_link.is_live(a, now=NOON)


def test_dead_after_sent_back():
    a = _confirmed()

    send_back(a, reason="Car broke down")
    a.refresh_from_db()

    assert not driver_link.is_live(a, now=NOON)


def test_dead_after_withdrawn():
    a = _confirmed()

    withdraw(a)
    a.refresh_from_db()

    assert not driver_link.is_live(a, now=NOON)


def test_dead_when_the_trip_is_cancelled():
    a = _confirmed()

    record_cancellation(a.reservation, Reservation.TripStatus.CANCELLED, reason="Customer called")
    a.refresh_from_db()

    assert not driver_link.is_live(a, now=NOON)


def test_dead_after_reassignment_even_to_the_same_vendor():
    a = _confirmed()
    vendor = a.vendor

    send_back(a, reason="Car broke down")
    new = assign_direct(a.reservation, vendor, payout=Decimal("100"))
    a.refresh_from_db()

    assert not driver_link.is_live(a, now=NOON)
    assert driver_link.is_live(new, now=NOON)


def test_live_until_twelve_hours_after_done():
    a = _confirmed()
    _status_at(a.reservation, DONE, NOON)
    a.reservation.refresh_from_db()

    assert driver_link.done_at(a.reservation) == NOON
    assert driver_link.is_live(a, now=NOON + timedelta(hours=11, minutes=59))
    assert not driver_link.is_live(a, now=NOON + timedelta(hours=12, minutes=1))


def test_moved_back_out_of_done_is_live_again():
    a = _confirmed()
    _status_at(a.reservation, DONE, NOON)
    _status_at(a.reservation, Reservation.TripStatus.ARRIVED, NOON + timedelta(hours=1))
    a.reservation.refresh_from_db()

    assert driver_link.done_at(a.reservation) is None
    assert driver_link.is_live(a, now=NOON + timedelta(hours=30))


def test_window_opens_six_hours_before_pickup():
    trip = ReservationFactory(
        pickup_date=date(2026, 11, 12),
        pickup_time=time(7, 30),
        pickup_timezone="America/New_York",
    )

    opens = driver_link.window_opens_at(trip)

    assert opens == datetime(2026, 11, 12, 1, 30, tzinfo=ZoneInfo("America/New_York"))
    assert not driver_link.window_open(trip, now=opens - timedelta(minutes=1))
    assert driver_link.window_open(trip, now=opens)


def test_no_pickup_date_never_opens():
    trip = ReservationFactory(pickup_date=None)

    assert driver_link.window_opens_at(trip) is None
    assert not driver_link.window_open(trip, now=NOON)
