"""Driver details updated: a follow-up when the details change after the first message went out."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.dispatch import services as dispatch_services
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.messaging import defaults, events, fields, sender
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.messaging.tests.test_driver_message import INFO, _confirmed, _due, _rows, _trip
from apps.messaging.touchpoint_templates import TEMPLATES
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
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        assert sender.run_touchpoints() == 1
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
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        sender.run_touchpoints()
    tp.refresh_from_db()
    assert tp.driver_details["cell"] == "+15715559999"


def test_two_changes_before_update_sends_one():
    res, a = _sent_trip()
    _new_cell(a, "+15715558888")
    _new_cell(a, "+15715557777")
    (up,) = _updates(res)
    _due(up)
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        sender.run_touchpoints()
    up.refresh_from_db()
    assert up.status == TouchPoint.Status.SENT
    assert up.driver_details["cell"] == "+15715557777"
    assert _updates(res).count() == 1


def test_change_after_update_sent_queues_another():
    res, a = _sent_trip()
    _new_cell(a, "+15715558888")
    (up,) = _updates(res)
    _due(up)
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        sender.run_touchpoints()
    _new_cell(a, "+15715557777")
    assert _updates(res).count() == 2
    assert _updates(res).filter(status=TouchPoint.Status.SCHEDULED).count() == 1


def test_same_details_resaved_no_update():
    res, a = _sent_trip()
    _new_cell(a, INFO["cell"])
    events.sync_waiting(res)
    assert not _updates(res).exists()


def test_in_house_reassignment_updates():
    res = _trip()
    first = dispatch_services.assign_in_house(res, DriverFactory(phone="+15715550101"))
    set_trip_status(res, "dispatched")
    (tp,) = _rows(res)
    _due(tp)
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        sender.run_touchpoints()
    dispatch_services.withdraw(first)
    dispatch_services.assign_in_house(res, DriverFactory(phone="+15715550202"))
    # Our own driver is Dispatched the moment they are picked (Moe, 2026-11-10), so the
    # customer hears about the new driver straight away.
    res.refresh_from_db()
    assert res.trip_status == "dispatched"
    assert _updates(res).count() == 1


def test_update_switch_off():
    MessageTemplate.objects.filter(key="driver_updated").update(enabled=False)
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
    with patch("apps.messaging.sender.podium.send_message") as send:
        sender.run_touchpoints()
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
    with patch("apps.messaging.sender.podium.send_message") as send:
        sender.run_touchpoints()
    up.refresh_from_db()
    assert (up.status, up.error) == (TouchPoint.Status.SKIPPED, "pickup passed")
    send.assert_not_called()


def test_update_is_editable():
    t = TEMPLATES["driver_updated"]
    assert (
        fields.validate(
            "driver_updated", subject=t.subject, email_body=t.email_body, sms_body=t.sms_body
        )
        == {}
    )
    default = defaults.DEFAULTS["driver_updated"]
    group = MessageTemplate.Section(default.section).label
    assert (default.name, group) == ("Driver details updated", "Trip day")
    assert t.subject == "Updated driver details for {trip_pickup_date}"
    assert "Your driver details have changed:" in t.email_body


def test_update_skipped_when_details_revert_before_it_sends():
    res, a = _sent_trip()
    _new_cell(a)
    (up,) = _updates(res)
    _new_cell(a, INFO["cell"])  # back to what the customer was told
    _due(up)
    with patch("apps.messaging.sender.podium.send_message") as send:
        sender.run_touchpoints()
    up.refresh_from_db()
    assert (up.status, up.error) == (TouchPoint.Status.SKIPPED, "no change to report")
    send.assert_not_called()
    assert _updates(res).count() == 1


def test_second_row_on_driver_changed_is_not_skipped():
    """Two rows on the trigger queue two notices for one change. The first to send must not
    make the other read as "no change": only a notice that reached someone before this one
    was queued counts as what the reader was last told."""
    from apps.messaging.factories import MessageRowFactory

    seeded = MessageTemplate.objects.get(key="driver_updated")
    MessageRowFactory(
        trigger="driver_changed",
        audience=seeded.audience,
        send_sms=seeded.send_sms,
        send_email=seeded.send_email,
        subject=seeded.subject,
        email_body=seeded.email_body,
        sms_body=seeded.sms_body,
        recipients=seeded.recipients,
        enabled=True,
    )
    res, a = _sent_trip()
    _new_cell(a)
    ups = list(TouchPoint.objects.filter(reservation=res, message__trigger="driver_changed"))
    assert len(ups) == 2
    for up in ups:
        _due(up)
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}):
        sender.run_touchpoints()
    for up in ups:
        up.refresh_from_db()
        assert (up.status, up.error) == (TouchPoint.Status.SENT, ""), up.error


def test_partly_sent_update_keeps_its_held_texts_after_a_revert():
    """Its email went with the new details; the texts held for quiet hours still go even if
    the details are then put back, so its readers hear the same story."""
    from apps.messaging.factories import DeliveryFactory
    from apps.messaging.models import Delivery

    res, a = _sent_trip()
    _new_cell(a)
    (up,) = _updates(res)
    a.refresh_from_db()
    up.driver_details = a.driver_info  # stamped by its email
    up.save(update_fields=["driver_details"])
    DeliveryFactory(
        touchpoint=up,
        message=up.message,
        channel=Delivery.Channel.EMAIL,
        address="dana@example.com",
        status=Delivery.Status.SENT,
    )
    _new_cell(a, INFO["cell"])  # back to what the first message said
    _due(up)
    with patch("apps.messaging.sender.podium.send_message", return_value={"uid": "m"}) as send:
        sender.run_touchpoints()
    up.refresh_from_db()
    assert up.error != "no change to report"
    assert up.status == TouchPoint.Status.SENT
    send.assert_called()
