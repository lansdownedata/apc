"""Review invites: Podium client call + LA-webhook done-trigger."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core import signing
from django.utils import timezone

from apps.integrations import podium
from apps.integrations.factories import LACustomerFactory, PodiumCredentialFactory
from apps.leads.factories import LeadFactory
from apps.messaging.models import TouchPoint
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _fresh_credential(access_token="AT"):
    return PodiumCredentialFactory(
        access_token=access_token, expires_at=timezone.now() + timedelta(hours=5)
    )


# --- podium.create_review_invitation ---------------------------------------
def test_create_review_invitation_posts_expected_body(settings):
    settings.PODIUM_LOCATION_UID = "loc-1"
    _fresh_credential()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"uid": "inv-1", "url": "https://podium.example/r/inv-1"}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        out = podium.create_review_invitation(
            phone="+15551234567", first_name="Jane", last_name="Doe"
        )
    assert out == {"uid": "inv-1", "url": "https://podium.example/r/inv-1"}
    method, url = req.call_args.args
    kwargs = req.call_args.kwargs
    assert method == "POST"
    assert url.endswith("/v4/review_invitations")
    assert kwargs["json"] == {
        "locationUid": "loc-1",
        "contact": {
            "phoneNumber": "+15551234567",
            "firstName": "Jane",
            "lastName": "Doe",
        },
    }


def test_create_review_invitation_honors_explicit_location_uid(settings):
    settings.PODIUM_LOCATION_UID = "loc-default"
    _fresh_credential()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        podium.create_review_invitation(
            phone="5551234567", first_name="A", last_name="B", location_uid="loc-override"
        )
    assert req.call_args.kwargs["json"]["locationUid"] == "loc-override"


# --- LA-webhook done-trigger -------------------------------------------------
def _url(la_customer):
    token = signing.dumps(la_customer.pk, salt="la-webhook")
    return f"/webhooks/limoanywhere/{token}/"


def _post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def test_last_trip_done_schedules_review_request(client):
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact)
    res = ReservationFactory(lead=lead, la_reservation_id="1")

    with patch("apps.integrations.views.touchpoints.schedule_review_request") as sched:
        _post(client, _url(lac), {"id": 1, "reservation_event": "reservation.completed"})

    res.refresh_from_db()
    assert res.trip_status == Reservation.TripStatus.DONE
    sched.assert_called_once_with(lead)


def test_non_terminal_status_does_not_schedule(client):
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact)
    ReservationFactory(lead=lead, la_reservation_id="2")

    with patch("apps.integrations.views.touchpoints.schedule_review_request") as sched:
        _post(client, _url(lac), {"id": 2, "reservation_event": "reservation.driver_was_assigned"})

    sched.assert_not_called()


def test_one_of_two_trips_done_does_not_schedule(client):
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact)
    ReservationFactory(lead=lead, la_reservation_id="3")
    ReservationFactory(
        lead=lead, la_reservation_id="4", trip_status=Reservation.TripStatus.UNASSIGNED
    )

    with patch("apps.integrations.views.touchpoints.schedule_review_request") as sched:
        _post(client, _url(lac), {"id": 3, "reservation_event": "reservation.completed"})

    sched.assert_not_called()


def test_done_plus_cancelled_pair_schedules(client):
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact)
    ReservationFactory(
        lead=lead, la_reservation_id="5", trip_status=Reservation.TripStatus.CANCELLED
    )
    ReservationFactory(lead=lead, la_reservation_id="6")

    with patch("apps.integrations.views.touchpoints.schedule_review_request") as sched:
        _post(client, _url(lac), {"id": 6, "reservation_event": "reservation.completed"})

    sched.assert_called_once_with(lead)


def test_replay_does_not_duplicate_touchpoint(client):
    """schedule_review_request itself is idempotent; replaying the webhook must not error
    or create a second row (verified against the real, un-mocked scheduler)."""
    lac = LACustomerFactory()
    lead = LeadFactory(contact=lac.contact)
    res = ReservationFactory(lead=lead, la_reservation_id="7")

    _post(client, _url(lac), {"id": 7, "reservation_event": "reservation.completed"})
    res.trip_status = ""
    res.save(update_fields=["trip_status"])
    _post(client, _url(lac), {"id": 7, "reservation_event": "reservation.completed"})

    assert TouchPoint.objects.filter(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST).count() == 1
