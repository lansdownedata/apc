"""Inbound LA webhook: signed-token auth, status writeback, LA-side change alerts."""

import json

import pytest
from django.core import signing

from apps.integrations.factories import LACustomerFactory
from apps.integrations.models import LAEvent
from apps.leads.factories import LeadFactory
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent

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


def test_invalid_json_returns_400(client):
    """Malformed JSON body should return 400 Bad Request."""
    lac = LACustomerFactory()
    resp = client.post(_url(lac), data=b"not-json", content_type="application/json")
    assert resp.status_code == 400
