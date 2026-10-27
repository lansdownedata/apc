"""Dispatching never waits on driver details; the customer message does."""

from datetime import date, time, timedelta

import pytest
from django.urls import reverse

from apps.dispatch import monitoring, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db

TS = Reservation.TripStatus
DRV = TouchPoint.Kind.DRIVER_RELEASED


def _name_only_trip():
    trip = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 8, 26),
        pickup_time=time(6, 15),
    )
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    services.set_driver_info(a, name="Sam Rivera", cell="", vehicle_desc="", vehicle_number="")
    return trip, a


def test_manual_dispatch_allowed_without_cell(logged_in_client):
    trip, _ = _name_only_trip()

    resp = logged_in_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
    )

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED
    assert TripStatusEvent.objects.filter(reservation=trip, status=TS.DISPATCHED).exists()


def test_driver_message_held_until_ready():
    trip, a = _name_only_trip()

    set_trip_status(trip, TS.DISPATCHED)
    assert not TouchPoint.objects.filter(reservation=trip, kind=DRV).exists()

    services.set_driver_info(
        a, name="Sam Rivera", cell="+17035550100", vehicle_desc="", vehicle_number=""
    )
    assert TouchPoint.objects.filter(reservation=trip, kind=DRV).count() == 1


def test_external_status_accepted_and_message_held():
    trip, _ = _name_only_trip()

    set_trip_status(trip, TS.DISPATCHED, source=TripStatusEvent.Source.LIMOANYWHERE)

    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED
    assert not TouchPoint.objects.filter(reservation=trip, kind=DRV).exists()


def test_monitor_keeps_alerting_after_dispatch():
    trip, _ = _name_only_trip()
    set_trip_status(trip, TS.DISPATCHED)
    trip.refresh_from_db()
    now = trip.pickup_at - timedelta(hours=5)

    result = monitoring.evaluate(trip, DispatchAlertConfig.load(), now)

    assert result[monitoring.K.NO_DRIVER_INFO] == monitoring.T.CRITICAL
