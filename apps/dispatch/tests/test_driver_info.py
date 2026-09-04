"""APC-21 — farmed-out driver + vehicle detail, and its release to the customer."""

from datetime import date, time, timedelta

import pytest
from django.urls import reverse

from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(**kwargs)


# --- services.set_driver_info ----------------------------------------------------------


def test_set_driver_info_saves_the_fields_and_triggers_release():
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    services.set_driver_info(
        a,
        name="Sam Rivera",
        cell="+15715551212",
        vehicle_desc="Black Suburban",
        vehicle_number="12",
    )

    a.refresh_from_db()
    assert a.driver_name == "Sam Rivera"
    assert a.driver_cell == "+15715551212"
    assert a.has_driver_info is True
    assert TouchPoint.objects.filter(
        reservation=trip, kind=TouchPoint.Kind.DRIVER_RELEASED
    ).exists()


def test_set_driver_info_refuses_in_house():
    trip = _trip()
    a = AssignmentFactory(reservation=trip, in_house=True)

    with pytest.raises(services.AssignmentError):
        services.set_driver_info(a, name="x", cell="", vehicle_desc="", vehicle_number="")


def test_set_driver_info_refuses_an_unconfirmed_offer():
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)

    with pytest.raises(services.AssignmentError):
        services.set_driver_info(a, name="x", cell="", vehicle_desc="", vehicle_number="")


def test_set_driver_info_does_not_release_twice():
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    services.set_driver_info(a, name="Sam Rivera", cell="", vehicle_desc="", vehicle_number="")

    services.set_driver_info(a, name="Sam Rivera", cell="", vehicle_desc="", vehicle_number="9")

    assert (
        TouchPoint.objects.filter(reservation=trip, kind=TouchPoint.Kind.DRIVER_RELEASED).count()
        == 1
    )


# --- view --------------------------------------------------------------------------


def test_driver_info_view_saves_and_normalises_the_cell(logged_in_client):
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.post(
        reverse("dispatch_driver_info", args=[a.pk]),
        {
            "driver_name": "Sam Rivera",
            "driver_cell": "(571) 555-1212",
            "vehicle_desc": "Black Suburban",
            "vehicle_number": "12",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    a.refresh_from_db()
    assert a.driver_cell == "+15715551212"


def test_driver_info_view_rejects_an_invalid_cell(logged_in_client):
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.post(
        reverse("dispatch_driver_info", args=[a.pk]),
        {"driver_name": "Sam Rivera", "driver_cell": "12345"},
    )

    assert resp.status_code == 400
    a.refresh_from_db()
    assert a.driver_name == ""


def test_driver_info_view_rejects_in_house(logged_in_client):
    trip = _trip()
    a = AssignmentFactory(reservation=trip, in_house=True)

    resp = logged_in_client.post(
        reverse("dispatch_driver_info", args=[a.pk]), {"driver_name": "Sam Rivera"}
    )

    assert resp.status_code == 400


# --- drawer surface ------------------------------------------------------------------


def test_panel_shows_the_driver_info_form_for_a_confirmed_farm_out(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Driver &amp; vehicle" in resp.content
    assert b"not on file" in resp.content


def test_panel_hides_the_driver_info_form_for_in_house(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, in_house=True)

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Driver &amp; vehicle" not in resp.content


def test_panel_omits_the_not_on_file_flag_once_entered(logged_in_client):
    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    services.set_driver_info(a, name="Sam Rivera", cell="", vehicle_desc="", vehicle_number="")

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"not on file" not in resp.content


# --- end-to-end with the monitor / config -----------------------------------------


def test_saving_driver_info_clears_the_evaluate_gap():
    from apps.dispatch import monitoring
    from apps.dispatch.models import DispatchAlertConfig

    trip = _trip()
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    cfg = DispatchAlertConfig.load()
    now = trip.pickup_at - timedelta(hours=1)
    assert monitoring.K.NO_DRIVER_INFO in monitoring.evaluate(trip, cfg, now)

    services.set_driver_info(a, name="Sam Rivera", cell="", vehicle_desc="", vehicle_number="")

    assert monitoring.K.NO_DRIVER_INFO not in monitoring.evaluate(trip, cfg, now)
