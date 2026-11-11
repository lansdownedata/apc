"""Driver link actions: status steps, circling and the no-show report."""

from datetime import UTC, date, datetime, time
from unittest.mock import patch

import pytest

from apps.dispatch import driver_link
from apps.dispatch.driver_link import (
    BeforeWindow,
    DeadLink,
    NotAllowed,
    StaleStatus,
    apply_status,
    back_status,
    next_status,
    report_no_show,
    set_driver_circling,
)
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DriverPing
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _live(status=S.DISPATCHED):
    """A confirmed assignment whose pickup is today, so the window is open at NOW."""
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 11, 12),
        pickup_time=time(9, 0),
    )
    assignment = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    set_trip_status(trip, status)
    return assignment


def _tap(assignment, to, from_, **kw):
    return apply_status(assignment.pk, to=to, from_=from_, now=NOW, **kw)


def test_sequence_helpers():
    assert next_status(S.DISPATCHED) == S.ON_THE_WAY
    assert next_status(S.DONE) is None
    assert next_status(S.ASSIGNED) is None
    assert back_status(S.ARRIVED) == S.ON_THE_WAY
    assert back_status(S.DISPATCHED) is None
    assert back_status(S.DONE) is None
    assert back_status(S.PENDING) is None


def test_next_step_is_allowed():
    a = _live()
    event = _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    a.reservation.refresh_from_db()
    assert a.reservation.trip_status == S.ON_THE_WAY
    assert event.source == TripStatusEvent.Source.DRIVER_LINK
    assert event.assignment_id == a.pk


def test_each_forward_step_through_done():
    a = _live()
    for frm, to in zip(driver_link.SEQUENCE, driver_link.SEQUENCE[1:], strict=False):
        _tap(a, to, frm)
    a.reservation.refresh_from_db()
    assert a.reservation.trip_status == S.DONE


def test_back_one_step_is_allowed():
    a = _live(S.ARRIVED)
    _tap(a, S.ON_THE_WAY, S.ARRIVED)
    a.reservation.refresh_from_db()
    assert a.reservation.trip_status == S.ON_THE_WAY


def test_cannot_go_back_from_dispatched():
    a = _live(S.DISPATCHED)
    with pytest.raises(NotAllowed):
        _tap(a, S.ASSIGNED, S.DISPATCHED)


def test_cannot_go_back_from_done():
    a = _live(S.ARRIVED)
    with patch("django.utils.timezone.now", return_value=NOW):
        set_trip_status(a.reservation, S.DONE)
    with pytest.raises(NotAllowed):
        _tap(a, S.CUSTOMER_IN_CAR, S.DONE)


def test_cannot_skip_a_step():
    a = _live()
    with pytest.raises(NotAllowed):
        _tap(a, S.ARRIVED, S.DISPATCHED)


def test_stale_from_raises_with_the_current_status():
    a = _live()
    _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    with pytest.raises(StaleStatus) as exc:
        _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    assert exc.value.current == S.ON_THE_WAY


def test_before_the_window_is_refused():
    a = _live()
    with pytest.raises(BeforeWindow) as exc:
        apply_status(
            a.pk, to=S.ON_THE_WAY, from_=S.DISPATCHED, now=datetime(2026, 11, 10, tzinfo=UTC)
        )
    assert exc.value.opens_at is not None


def test_dead_link_is_refused():
    a = _live()
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status", "updated_at"])
    with pytest.raises(DeadLink):
        _tap(a, S.ON_THE_WAY, S.DISPATCHED)


def test_trip_back_at_assigned_offers_no_step():
    a = _live(S.ASSIGNED)
    assert next_status(S.ASSIGNED) is None
    with pytest.raises(NotAllowed):
        _tap(a, S.DISPATCHED, S.ASSIGNED)


def test_on_the_way_queues_the_customer_message():
    MessageTemplate.objects.filter(key="status_on_the_way").update(enabled=True)
    a = _live()
    _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    assert TouchPoint.objects.filter(
        reservation=a.reservation, kind=TouchPoint.Kind.STATUS_ON_THE_WAY
    ).exists()


def test_status_tap_with_coords_writes_a_status_ping():
    a = _live()
    event = _tap(a, S.ON_THE_WAY, S.DISPATCHED, coords={"lat": 38.9, "lng": -77.0, "accuracy": 12})
    ping = DriverPing.objects.get()
    assert ping.kind == DriverPing.Kind.STATUS
    assert ping.status_event_id == event.pk
    assert ping.assignment_id == a.pk
    assert ping.reservation_id == a.reservation_id


def test_status_tap_without_coords_writes_no_ping():
    a = _live()
    _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    assert not DriverPing.objects.exists()


def test_circling_allowed_on_the_way_and_arrived_only():
    a = _live(S.DISPATCHED)
    with pytest.raises(NotAllowed):
        set_driver_circling(a.pk, on=True)
    _tap(a, S.ON_THE_WAY, S.DISPATCHED)
    set_driver_circling(a.pk, on=True)
    a.reservation.refresh_from_db()
    assert a.reservation.circling_at is not None
    assert a.reservation.circling_source == Reservation.CirclingSource.DRIVER_LINK
    set_driver_circling(a.pk, on=False)
    a.reservation.refresh_from_db()
    assert a.reservation.circling_at is None


def test_circling_on_a_dead_link_is_refused():
    a = _live(S.ARRIVED)
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status", "updated_at"])
    with pytest.raises(DeadLink):
        set_driver_circling(a.pk, on=True)


def test_customer_in_car_clears_circling():
    a = _live(S.ARRIVED)
    set_driver_circling(a.pk, on=True)
    _tap(a, S.CUSTOMER_IN_CAR, S.ARRIVED)
    a.reservation.refresh_from_db()
    assert a.reservation.circling_at is None


def test_no_show_report_notifies_and_keeps_status():
    a = _live(S.ARRIVED)
    report_no_show(a.pk, note="x" * 600)
    trip = a.reservation
    trip.refresh_from_db()
    assert trip.trip_status == S.ARRIVED
    assert trip.driver_no_show_at is not None
    assert len(trip.driver_no_show_note) == 500
    note = Notification.objects.get(kind=Notification.Kind.DRIVER_REPORT)
    assert trip.reference in note.title
    assert len(note.detail) == 255


def test_no_show_on_a_dead_link_is_refused():
    a = _live(S.ARRIVED)
    a.status = Assignment.Status.WITHDRAWN
    a.save(update_fields=["status", "updated_at"])
    with pytest.raises(DeadLink):
        report_no_show(a.pk)
