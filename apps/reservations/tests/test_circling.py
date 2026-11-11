"""Trip-status cleanup: Circling is a notice on the trip, not a status."""

import json

import pytest
from django.core import signing
from django.urls import reverse

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.integrations.factories import LACustomerFactory
from apps.leads.factories import LeadFactory
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_circling, set_trip_status

pytestmark = pytest.mark.django_db

TS = Reservation.TripStatus
Source = Reservation.CirclingSource


def test_turning_on_stamps_the_notice(admin_user):
    trip = ReservationFactory(trip_status=TS.ARRIVED)

    set_circling(trip, on=True, source=Source.MANUAL, user=admin_user)

    trip.refresh_from_db()
    assert trip.circling_at is not None
    assert trip.circling_source == Source.MANUAL
    assert trip.circling_by == admin_user


@pytest.mark.parametrize("status", [TS.ON_THE_WAY, TS.ARRIVED])
def test_allowed_on_the_way_and_arrived(status):
    trip = ReservationFactory(trip_status=status)
    set_circling(trip, on=True, source=Source.DRIVER_LINK)
    trip.refresh_from_db()
    assert trip.circling_at is not None
    assert trip.circling_by is None


@pytest.mark.parametrize("status", [TS.PENDING, TS.UNASSIGNED, TS.DISPATCHED, TS.DONE])
def test_refused_outside_on_the_way_and_arrived(status):
    trip = ReservationFactory(trip_status=status)
    with pytest.raises(ValueError):
        set_circling(trip, on=True, source=Source.MANUAL)
    trip.refresh_from_db()
    assert trip.circling_at is None


def test_turning_on_again_keeps_the_original_time():
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.DRIVER_LINK)
    trip.refresh_from_db()
    first = trip.circling_at

    set_circling(trip, on=True, source=Source.MANUAL)

    trip.refresh_from_db()
    assert trip.circling_at == first
    assert trip.circling_source == Source.DRIVER_LINK


def test_turning_off_blanks_the_notice(admin_user):
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.MANUAL, user=admin_user)

    set_circling(trip, on=False, source=Source.MANUAL)

    trip.refresh_from_db()
    assert trip.circling_at is None
    assert trip.circling_source == ""
    assert trip.circling_by is None


@pytest.mark.parametrize("status", [TS.CUSTOMER_IN_CAR, TS.DONE, *sorted(CANCELLED_STATUSES)])
def test_the_notice_clears_when_the_trip_moves_on(status):
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.MANUAL)

    set_trip_status(trip, status)

    trip.refresh_from_db()
    assert trip.circling_at is None
    assert trip.circling_source == ""


def test_the_notice_survives_a_move_to_on_the_way():
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.MANUAL)

    set_trip_status(trip, TS.ON_THE_WAY)

    trip.refresh_from_db()
    assert trip.circling_at is not None


def test_the_notice_survives_a_move_from_on_the_way_to_arrived():
    trip = ReservationFactory(trip_status=TS.ON_THE_WAY)
    set_circling(trip, on=True, source=Source.MANUAL)

    set_trip_status(trip, TS.ARRIVED)

    trip.refresh_from_db()
    assert trip.circling_at is not None


@pytest.mark.parametrize("status", [TS.DISPATCHED, TS.UNASSIGNED, TS.ASSIGNED, TS.PENDING])
def test_the_notice_clears_when_the_trip_moves_back(status):
    """Circling only means something On The Way or Arrived; any other status clears it."""
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.MANUAL)

    set_trip_status(trip, status)

    trip.refresh_from_db()
    assert trip.circling_at is None
    assert trip.circling_source == ""


def test_sent_back_clears_the_notice():
    from apps.dispatch import services
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment
    from apps.leads.models import Lead
    from apps.vendors.factories import VendorFactory

    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Source.MANUAL)
    assignment = AssignmentFactory(
        reservation=trip, vendor=VendorFactory(), status=Assignment.Status.CONFIRMED
    )

    services.send_back(assignment, reason="Car broke down")

    trip.refresh_from_db()
    assert trip.trip_status == TS.UNASSIGNED
    assert trip.circling_at is None


def test_drawer_arrived_to_dispatched_clears_the_notice(admin_client):
    from apps.dispatch.factories import AssignmentFactory
    from apps.dispatch.models import Assignment
    from apps.leads.models import Lead
    from apps.vendors.factories import VendorFactory

    trip = ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), trip_status=TS.ARRIVED)
    AssignmentFactory(reservation=trip, vendor=VendorFactory(), status=Assignment.Status.CONFIRMED)
    set_circling(trip, on=True, source=Source.MANUAL)

    resp = admin_client.post(
        reverse("dispatch_set_status", args=[trip.pk]), {"status": TS.DISPATCHED}
    )

    assert resp.status_code == 200, resp.content
    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED
    assert trip.circling_at is None


def test_la_circling_event_sets_arrived_and_the_notice(client):
    lac = LACustomerFactory()
    trip = ReservationFactory(
        lead=LeadFactory(contact=lac.contact), la_reservation_id="555", trip_status=TS.ON_THE_WAY
    )
    token = signing.dumps(lac.pk, salt="la-webhook")

    resp = client.post(
        f"/webhooks/limoanywhere/{token}/",
        data=json.dumps(
            {"id": 555, "reservation_event": "reservation.driver_arrived_at_pickup_and_circling"}
        ),
        content_type="application/json",
    )

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.trip_status == TS.ARRIVED
    assert trip.circling_at is not None
    assert trip.circling_source == Source.LIMOANYWHERE
    assert TripStatusEvent.objects.filter(
        reservation=trip, status=TS.ARRIVED, source=TripStatusEvent.Source.LIMOANYWHERE
    ).exists()


def test_la_circling_event_on_an_arrived_trip_still_sets_the_notice(client):
    lac = LACustomerFactory()
    trip = ReservationFactory(
        lead=LeadFactory(contact=lac.contact), la_reservation_id="556", trip_status=TS.ARRIVED
    )
    token = signing.dumps(lac.pk, salt="la-webhook")

    client.post(
        f"/webhooks/limoanywhere/{token}/",
        data=json.dumps(
            {"id": 556, "reservation_event": "reservation.driver_arrived_at_pickup_and_circling"}
        ),
        content_type="application/json",
    )

    trip.refresh_from_db()
    assert trip.circling_source == Source.LIMOANYWHERE


def test_drawer_endpoint_toggles_the_notice(admin_client):
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    url = reverse("dispatch_circling", args=[trip.pk])

    resp = admin_client.post(url, {"on": "1"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    trip.refresh_from_db()
    assert trip.circling_at is not None
    assert trip.circling_source == Source.MANUAL
    assert trip.circling_by is not None

    resp = admin_client.post(url, {"on": "0"})

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.circling_at is None


def test_drawer_endpoint_refuses_a_wrong_status(admin_client):
    trip = ReservationFactory(trip_status=TS.DISPATCHED)

    resp = admin_client.post(reverse("dispatch_circling", args=[trip.pk]), {"on": "1"})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    trip.refresh_from_db()
    assert trip.circling_at is None


@pytest.mark.parametrize("status", [TS.DONE, TS.CUSTOMER_IN_CAR, sorted(CANCELLED_STATUSES)[0]])
def test_a_late_la_circling_event_leaves_a_finished_trip_alone(client, status):
    lac = LACustomerFactory()
    trip = ReservationFactory(
        lead=LeadFactory(contact=lac.contact), la_reservation_id="777", trip_status=status
    )
    token = signing.dumps(lac.pk, salt="la-webhook")

    resp = client.post(
        f"/webhooks/limoanywhere/{token}/",
        data=json.dumps(
            {"id": 777, "reservation_event": "reservation.driver_arrived_at_pickup_and_circling"}
        ),
        content_type="application/json",
    )

    assert resp.status_code == 200
    trip.refresh_from_db()
    assert trip.trip_status == status
    assert trip.circling_at is None
    assert not TripStatusEvent.objects.filter(reservation=trip).exists()
