"""Inbound LA webhook: signed-token auth, status writeback, LA-side change alerts."""

import json
from unittest.mock import patch

import pytest
from django.core import signing

from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.integrations.factories import LACustomerFactory
from apps.integrations.models import LAEvent
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _url(la_customer):
    token = signing.dumps(la_customer.pk, salt="la-webhook")
    return f"/webhooks/limoanywhere/{token}/"


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def test_bad_token_is_404(client):
    resp = _post(client, "/webhooks/limoanywhere/forged-token/", {"id": 1})
    assert resp.status_code == 404


def test_driver_assigned_updates_trip_status(client):
    lac = LACustomerFactory()
    res = ReservationFactory(lead=LeadFactory(contact=lac.contact), la_reservation_id="67890")
    resp = _post(
        client,
        _url(lac),
        {
            "id": 67890,
            "reservation_event": "reservation.driver_was_assigned",
        },
    )
    assert resp.status_code == 200
    res.refresh_from_db()
    assert res.trip_status == Reservation.TripStatus.ASSIGNED
    assert TripStatusEvent.objects.filter(
        reservation=res, source=TripStatusEvent.Source.LIMOANYWHERE
    ).exists()
    assert LAEvent.objects.filter(reservation=res).exists()


def test_cancelled_in_la_raises_alert(client):
    lac = LACustomerFactory()
    res = ReservationFactory(lead=LeadFactory(contact=lac.contact), la_reservation_id="1")
    _post(client, _url(lac), {"id": 1, "reservation_event": "reservation.cancelled"})
    res.refresh_from_db()
    assert res.trip_status == Reservation.TripStatus.CANCELLED
    assert Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_cancelled_in_la_releases_the_affiliate(client):
    """LimoAnywhere is the system of record, so a cancellation there ends the trip —
    but the branch only wrote trip_status and notified. On the GNet channel that left a
    real affiliate holding a live booking for a trip that no longer exists."""
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact, status=Lead.Status.BOOKED)
    res = ReservationFactory(lead=lead, la_reservation_id="3")
    assignment = AssignmentFactory(
        reservation=res,
        vendor=VendorFactory(gnet_grid_id="gnet-1"),
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
        status=Assignment.Status.CONFIRMED,
    )

    resp = _post(client, _url(lac), {"id": 3, "reservation_event": "reservation.cancelled"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.WITHDRAWN


def test_a_gateway_failure_never_500s_the_la_webhook(client):
    """An LA webhook must not fail because a GNet cancel did — LA would retry a
    cancellation that has already been applied locally."""
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact, status=Lead.Status.BOOKED)
    res = ReservationFactory(lead=lead, la_reservation_id="4")
    AssignmentFactory(
        reservation=res,
        vendor=VendorFactory(gnet_grid_id="gnet-1"),
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
        status=Assignment.Status.CONFIRMED,
    )

    with patch.object(dispatch_services.gnet_sync, "cancel_assignment", side_effect=OSError):
        resp = _post(client, _url(lac), {"id": 4, "reservation_event": "reservation.cancelled"})

    assert resp.status_code == 200
    res.refresh_from_db()
    assert res.trip_status == Reservation.TripStatus.CANCELLED


def test_updated_in_la_does_not_release_the_affiliate(client):
    """An edit is not a cancellation — the affiliate keeps the trip."""
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact, status=Lead.Status.BOOKED)
    res = ReservationFactory(lead=lead, la_reservation_id="5")
    assignment = AssignmentFactory(
        reservation=res, status=Assignment.Status.CONFIRMED, channel=Assignment.Channel.MANUAL
    )

    _post(client, _url(lac), {"id": 5, "reservation_event": "reservation.updated"})

    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_updated_in_la_raises_alert_without_status_change(client):
    lac = LACustomerFactory()
    res = ReservationFactory(lead=LeadFactory(contact=lac.contact), la_reservation_id="2")
    before = res.trip_status
    _post(client, _url(lac), {"id": 2, "reservation_event": "reservation.updated"})
    res.refresh_from_db()
    assert res.trip_status == before
    assert Notification.objects.filter(kind=Notification.Kind.LA_CHANGED).exists()


def test_unknown_reservation_logged_and_ignored(client):
    lac = LACustomerFactory()
    resp = _post(client, _url(lac), {"id": 999, "reservation_event": "reservation.booked"})
    assert resp.status_code == 200
    assert LAEvent.objects.filter(reservation=None).exists()


def test_payload_without_id_ignores_unpushed_reservation(client):
    """Payload without id should NOT match un-pushed reservation (la_reservation_id="")."""
    lac = LACustomerFactory()
    res = ReservationFactory(
        lead=LeadFactory(contact=lac.contact)
    )  # la_reservation_id="" by default
    before_status = res.trip_status
    resp = _post(client, _url(lac), {"reservation_event": "reservation.driver_was_assigned"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    res.refresh_from_db()
    assert res.trip_status == before_status  # unchanged
    # logged with reservation=None
    assert LAEvent.objects.filter(reservation=None).exists()


def test_token_cannot_write_another_customers_reservation(client):
    """A customer's signed token must not let it match a reservation belonging to
    a different customer that happens to share the same la_reservation_id id space."""
    lac_a = LACustomerFactory()
    lac_b = LACustomerFactory()
    res_b = ReservationFactory(lead=LeadFactory(contact=lac_b.contact), la_reservation_id="55")
    before_status = res_b.trip_status

    resp = _post(
        client, _url(lac_a), {"id": 55, "reservation_event": "reservation.driver_was_assigned"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    res_b.refresh_from_db()
    assert res_b.trip_status == before_status
    event = LAEvent.objects.filter(la_customer=lac_a, reservation=None).first()
    assert event is not None
    assert event.payload["id"] == 55


def test_invalid_json_returns_400(client):
    """Malformed JSON body should return 400 Bad Request."""
    lac = LACustomerFactory()
    resp = client.post(_url(lac), data=b"not-json", content_type="application/json")
    assert resp.status_code == 400
