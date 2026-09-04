"""APC-22 — the dispatch drawer's manual Trip status control."""

from datetime import date, time

import pytest
from django.urls import reverse

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import NotificationConfig, TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent

pytestmark = pytest.mark.django_db

TS = Reservation.TripStatus


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("pickup_date", date(2026, 8, 26))
    kwargs.setdefault("pickup_time", time(6, 15))
    return ReservationFactory(**kwargs)


# --- view --------------------------------------------------------------------------


def test_set_status_writes_the_trip_status_and_an_event(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
    )

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED
    event = TripStatusEvent.objects.get(reservation=trip)
    assert event.source == TripStatusEvent.Source.MANUAL
    assert event.changed_by is not None


def test_set_status_refuses_an_uncovered_trip(logged_in_client):
    trip = _trip()

    resp = logged_in_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
    )

    assert resp.status_code == 400
    trip.refresh_from_db()
    assert trip.trip_status == ""


def test_set_status_refuses_an_offer_not_yet_confirmed(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)

    resp = logged_in_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
    )

    assert resp.status_code == 400


def test_set_status_refuses_a_status_outside_the_curated_set(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.post(reverse("dispatch_set_status", args=[trip.pk]), {"status": "done"})

    assert resp.status_code == 400
    trip.refresh_from_db()
    assert trip.trip_status == ""


def test_set_status_fires_the_configured_customer_notification(logged_in_client):
    cfg = NotificationConfig.load()
    cfg.status_dispatched_enabled = True
    cfg.save()
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    logged_in_client.post(reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"})

    assert TouchPoint.objects.filter(
        reservation=trip, kind=TouchPoint.Kind.STATUS_DISPATCHED
    ).exists()


def test_set_status_is_idempotent_on_the_same_status(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    logged_in_client.post(reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"})

    resp = logged_in_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": "dispatched"}
    )

    assert resp.status_code == 200
    assert TripStatusEvent.objects.filter(reservation=trip).count() == 1


# --- drawer surface ------------------------------------------------------------------


def test_panel_shows_the_status_buttons_for_a_confirmed_trip(logged_in_client):
    trip = _trip()
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Trip status" in resp.content
    assert b"On The Way" in resp.content
    assert b"Arrived" in resp.content


def test_panel_hides_the_status_buttons_for_an_uncovered_trip(logged_in_client):
    trip = _trip()

    resp = logged_in_client.get(reverse("dispatch_assign_panel", args=[trip.pk]))

    assert b"Trip status" not in resp.content
