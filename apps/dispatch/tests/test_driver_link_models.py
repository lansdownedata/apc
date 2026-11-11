"""Driver link: the data the throwaway driver page writes (ping model, status source)."""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from django.urls import ResolverMatch

from apps.audit.middleware import source_for
from apps.audit.models import AuditEntry
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig, DriverPing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_trip_status

pytestmark = pytest.mark.django_db


def test_set_trip_status_records_the_assignment():
    trip = ReservationFactory()
    set_trip_status(trip, Reservation.TripStatus.DISPATCHED)
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)

    event = set_trip_status(
        trip,
        Reservation.TripStatus.ON_THE_WAY,
        source=TripStatusEvent.Source.DRIVER_LINK,
        assignment=a,
    )

    assert event.assignment == a
    assert event.source == "driver_link"


def test_driver_ping_defaults():
    a = AssignmentFactory(status=Assignment.Status.CONFIRMED)

    ping = DriverPing.objects.create(
        assignment=a,
        reservation=a.reservation,
        kind=DriverPing.Kind.TRACK,
        latitude="38.851200",
        longitude="-77.040200",
    )

    assert ping.created_at is not None
    assert ping.status_event is None


def test_passenger_contact_setting_defaults_on():
    assert DispatchAlertConfig.load().driver_page_show_passenger_contact is True


def test_route_fields_are_not_audited():
    trip = ReservationFactory()
    trip.route_geometry = "encoded-polyline"
    trip.route_signature = "abc123"
    trip.save(update_fields=["route_geometry", "route_signature"])

    ct = ContentType.objects.get_for_model(Reservation)
    for entry in AuditEntry.objects.filter(content_type=ct, object_id=trip.pk):
        assert "route_geometry" not in (entry.changes or {})
        assert "route_signature" not in (entry.changes or {})


def test_driver_link_requests_are_labelled():
    request = RequestFactory().post("/driver/abc/status/")
    request.resolver_match = ResolverMatch(lambda r: None, (), {}, url_name="driver_link_status")

    assert source_for(request) == "driver: trip link"
