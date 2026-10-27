"""The driver message: waits for Dispatched + ready details, sends N hours before pickup."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

K = TouchPoint.Kind


@pytest.fixture(autouse=True)
def _engine_on(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"


INFO = {
    "name": "Sam Rivera",
    "cell": "+15715551212",
    "vehicle_desc": "Suburban",
    "vehicle_number": "9",
}


def _trip(*, hours=72):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    lead.contact.phone = "+15715550001"
    lead.contact.save()
    res = ReservationFactory(lead=lead, pickup_date=timezone.now().date(), pickup_time=None)
    when = (timezone.now() + timedelta(hours=hours)).astimezone(res.pickup_at.tzinfo)
    res.pickup_date, res.pickup_time = when.date(), when.time().replace(microsecond=0)
    res.save()
    return res


def _confirmed(res, **over):
    a = AssignmentFactory(reservation=res, status=Assignment.Status.CONFIRMED)
    dispatch_services.set_driver_info(
        a,
        name=over.get("name", INFO["name"]),
        cell=over.get("cell", INFO["cell"]),
        vehicle_desc=INFO["vehicle_desc"],
        vehicle_number=INFO["vehicle_number"],
    )
    return a


def _rows(res):
    return TouchPoint.objects.filter(reservation=res, kind=K.DRIVER_RELEASED)


def _due(tp):
    tp.scheduled_for = timezone.now() - timedelta(minutes=1)
    tp.save(update_fields=["scheduled_for"])


def test_dispatched_early_sends_at_pickup_minus_12h():
    res = _trip(hours=72)
    _confirmed(res)
    assert not _rows(res).exists()
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    assert tp.scheduled_for == res.pickup_at - timedelta(hours=12)


def test_dispatched_late_sends_now():
    res = _trip(hours=6)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    assert abs((tp.scheduled_for - timezone.now()).total_seconds()) < 5


def test_same_save_one_message():
    res = _trip()
    a = _confirmed(res)
    set_trip_status(res, "dispatched")
    dispatch_services.set_driver_info(
        a, name="Sam Rivera", cell="+15715551212", vehicle_desc="Suburban", vehicle_number="10"
    )
    assert _rows(res).count() == 1


def test_not_dispatched_no_message():
    res = _trip()
    _confirmed(res)
    set_trip_status(res, "assigned")
    assert not _rows(res).exists()


def test_held_when_details_cleared_at_send():
    res = _trip()
    a = _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    Assignment.objects.filter(pk=a.pk).update(driver_cell="")
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        touchpoints.run_touchpoints()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED
    send.assert_not_called()


def test_held_when_status_drops():
    res = _trip()
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    set_trip_status(res, "assigned")
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        touchpoints.run_touchpoints()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED
    send.assert_not_called()


def test_pickup_passed_skips():
    res = _trip(hours=72)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    past = timezone.now() - timedelta(hours=1)
    res.pickup_date, res.pickup_time = past.date(), past.time()
    res.pickup_timezone = "UTC"
    res.save()
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        touchpoints.run_touchpoints()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, "pickup passed")
    send.assert_not_called()


def test_pickup_moved_redates_scheduled():
    res = _trip(hours=72)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    res.pickup_date = res.pickup_date - timedelta(days=1)
    res.save()
    touchpoints.sync_driver_message(res)
    (again,) = _rows(res)
    assert again.pk == tp.pk
    assert again.scheduled_for == res.pickup_at - timedelta(hours=12)


def test_sent_not_resent_on_pickup_move():
    res = _trip(hours=72)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    TouchPoint.objects.filter(pk=tp.pk).update(status=TouchPoint.Status.SENT)
    res.pickup_date = res.pickup_date - timedelta(days=1)
    res.save()
    assert touchpoints.sync_driver_message(res) is None
    assert _rows(res).count() == 1


def test_in_house_gets_message():
    res = _trip()
    dispatch_services.assign_in_house(res, DriverFactory(phone="+15715550101"))
    set_trip_status(res, "dispatched")
    assert _rows(res).count() == 1


def test_dispatched_status_message_retired():
    res = _trip()
    MessageTemplate.objects.create(kind="status_dispatched", enabled=True)
    set_trip_status(res, "dispatched")
    assert not TouchPoint.objects.filter(kind=K.STATUS_DISPATCHED).exists()


def test_sent_details_recorded():
    res = _trip(hours=6)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        assert touchpoints.run_touchpoints() == 1
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    assert tp.driver_details == INFO


def test_editable_timing_applies():
    MessageTemplate.objects.create(kind="driver_released", timing_amount=4, timing_unit="hours")
    res = _trip(hours=72)
    _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    assert tp.scheduled_for == res.pickup_at - timedelta(hours=4)
