"""Driver details updated: a follow-up when the details change after the first message went out."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.dispatch import services as dispatch_services
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.messaging import fields, touchpoints
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.overrides import MESSAGES
from apps.messaging.tests.test_driver_message import INFO, _confirmed, _due, _rows, _trip
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

K = TouchPoint.Kind


@pytest.fixture(autouse=True)
def _engine_on(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"


def _updates(res):
    return TouchPoint.objects.filter(reservation=res, kind=K.DRIVER_UPDATED)


def _sent_trip(hours=72):
    """A dispatched trip whose driver message has already gone out."""
    res = _trip(hours=hours)
    a = _confirmed(res)
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        assert touchpoints.run_touchpoints() == 1
    return res, a


def _new_cell(a, cell="+15715559999"):
    dispatch_services.set_driver_info(
        a,
        name=INFO["name"],
        cell=cell,
        vehicle_desc=INFO["vehicle_desc"],
        vehicle_number=INFO["vehicle_number"],
    )


def test_change_after_sent_queues_update():
    res, a = _sent_trip()
    _new_cell(a)
    (up,) = _updates(res)
    assert up.status == TouchPoint.Status.SCHEDULED
    assert abs((up.scheduled_for - timezone.now()).total_seconds()) < 5


def test_change_before_sent_no_update():
    res = _trip()
    a = _confirmed(res)
    set_trip_status(res, "dispatched")
    _new_cell(a)
    assert not _updates(res).exists()
    (tp,) = _rows(res)
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        touchpoints.run_touchpoints()
    tp.refresh_from_db()
    assert tp.driver_details["cell"] == "+15715559999"


def test_two_changes_before_update_sends_one():
    res, a = _sent_trip()
    _new_cell(a, "+15715558888")
    _new_cell(a, "+15715557777")
    (up,) = _updates(res)
    _due(up)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        touchpoints.run_touchpoints()
    up.refresh_from_db()
    assert up.status == TouchPoint.Status.SENT
    assert up.driver_details["cell"] == "+15715557777"
    assert _updates(res).count() == 1


def test_change_after_update_sent_queues_another():
    res, a = _sent_trip()
    _new_cell(a, "+15715558888")
    (up,) = _updates(res)
    _due(up)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        touchpoints.run_touchpoints()
    _new_cell(a, "+15715557777")
    assert _updates(res).count() == 2
    assert _updates(res).filter(status=TouchPoint.Status.SCHEDULED).count() == 1


def test_same_details_resaved_no_update():
    res, a = _sent_trip()
    _new_cell(a, INFO["cell"])
    touchpoints.sync_driver_message(res)
    assert not _updates(res).exists()


def test_in_house_reassignment_updates():
    res = _trip()
    first = dispatch_services.assign_in_house(res, DriverFactory(phone="+15715550101"))
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "m"}):
        touchpoints.run_touchpoints()
    dispatch_services.withdraw(first)
    dispatch_services.assign_in_house(res, DriverFactory(phone="+15715550202"))
    assert _updates(res).count() == 1


def test_update_switch_off():
    MessageTemplate.objects.create(kind="driver_updated", enabled=False)
    res, a = _sent_trip()
    _new_cell(a)
    assert not _updates(res).exists()


def test_unknown_original_details_no_update():
    res, a = _sent_trip()
    _rows(res).update(driver_details=None)
    _new_cell(a)
    assert not _updates(res).exists()


def test_update_held_when_details_cleared_at_send():
    res, a = _sent_trip()
    _new_cell(a)
    (up,) = _updates(res)
    _due(up)
    Assignment.objects.filter(pk=a.pk).update(driver_cell="")
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        touchpoints.run_touchpoints()
    up.refresh_from_db()
    assert up.status == TouchPoint.Status.SCHEDULED
    send.assert_not_called()


def test_update_skipped_when_pickup_passed():
    res, a = _sent_trip()
    _new_cell(a)
    (up,) = _updates(res)
    _due(up)
    past = timezone.now() - timedelta(hours=1)
    res.pickup_date, res.pickup_time, res.pickup_timezone = past.date(), past.time(), "UTC"
    res.save()
    with patch("apps.messaging.touchpoints.podium.send_message") as send:
        touchpoints.run_touchpoints()
    up.refresh_from_db()
    assert (up.status, up.error) == (TouchPoint.Status.SKIPPED, "pickup passed")
    send.assert_not_called()


def test_update_is_editable():
    t = touchpoints.overrides.default("driver_updated")
    assert (
        fields.validate(
            "driver_updated", subject=t.subject, email_body=t.email_body, sms_body=t.sms_body
        )
        == {}
    )
    (spec,) = [m for m in MESSAGES if m.kind == "driver_updated"]
    assert (spec.label, spec.group) == ("Driver details updated", "Trip day")
    assert t.subject == "Updated driver details for {trip_pickup_date}"
    assert "Your driver details have changed:" in t.email_body
