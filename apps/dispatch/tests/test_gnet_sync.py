"""Tests for the GNet orchestration layer (apps.dispatch.gnet_sync).

SAFETY: the GNet gateway is deployed in production and talks to real GNet — a
successful send books a REAL vehicle with a REAL affiliate. Every test here mocks
either the client functions (`send_trip` / `cancel_trip`) at the `gnet_sync` boundary
via `patch.object(gnet_sync, ...)`, or — for the transport-failure tests, which
deliberately exercise `gnet.py`'s own exception handling rather than bypassing it —
`requests.request` itself via `patch.object(gnet, "requests")`. Neither performs real
network I/O.
"""

from unittest.mock import patch

import pytest
import requests

import apps.dispatch.gnet_sync as gnet_sync
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import GnetEvent
from apps.integrations import gnet
from apps.integrations.gnet import GnetAPIError, build_send_payload
from apps.leads.factories import VehicleTypeFactory
from apps.notifications.models import Notification
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _assignment(*, gnet_grid_id="gnet-partner-42", vehicle_name="Luxury Sedan", **kwargs):
    """An assignment that build_send_payload can turn into a valid GNet payload."""
    vendor = VendorFactory(gnet_grid_id=gnet_grid_id)
    vehicle = VehicleTypeFactory(name=vehicle_name)
    reservation = ReservationFactory(vehicle=vehicle)
    return AssignmentFactory(reservation=reservation, vendor=vendor, **kwargs)


def _arm(settings):
    """Flip both preview-gating flags off so a real send would be attempted."""
    settings.GNET_ACTIVE = True
    settings.GNET_API_KEY = "lds_testkey1234567890"


# --- preview mode: no HTTP call whatsoever ---


def test_preview_when_gnet_inactive_even_with_key_present(settings):
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    assignment = _assignment()
    expected_payload = build_send_payload(assignment)

    with patch.object(gnet_sync, "send_trip") as mock_send:
        event = gnet_sync.push_assignment(assignment)

    assert not mock_send.called
    assert event.result == GnetEvent.Result.PREVIEW
    assert event.payload == expected_payload
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""


def test_preview_when_key_blank_even_if_active(settings):
    settings.GNET_ACTIVE = True
    settings.GNET_API_KEY = ""
    assignment = _assignment()

    with patch.object(gnet_sync, "send_trip") as mock_send:
        event = gnet_sync.push_assignment(assignment)

    assert not mock_send.called
    assert event.result == GnetEvent.Result.PREVIEW


def test_a_previewed_offer_alerts_instead_of_vanishing(settings):
    """The assignment is created OFFERED either way, so a preview used to leave the
    trip looking farmed out with nothing sent and nothing said — visible only in Django
    admin. The moment someone types a griddID onto a vendor record (a normal admin
    action, no deploy) that affiliate's offers would disappear into this hole."""
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    assignment = _assignment()

    with patch.object(gnet_sync, "send_trip") as mock_send:
        gnet_sync.push_assignment(assignment)

    assert not mock_send.called
    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "preview" in f"{notification.title} {notification.detail}".lower()


def test_a_previewed_cancel_alerts_too(settings):
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    assignment = _assignment(gnet_transaction_id="TX-existing")

    with patch.object(gnet_sync, "cancel_trip") as mock_cancel:
        gnet_sync.cancel_assignment(assignment)

    assert not mock_cancel.called
    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "preview" in f"{notification.title} {notification.detail}".lower()


# --- successful send ---


def test_success_stores_transaction_id_and_marks_event_success(settings):
    _arm(settings)
    assignment = _assignment()

    with patch.object(
        gnet_sync,
        "send_trip",
        return_value={"transactionId": "TX-1", "reservationId": "R-1", "totalAmount": "150.00"},
    ) as mock_send:
        event = gnet_sync.push_assignment(assignment)

    assert mock_send.called
    assert event.result == GnetEvent.Result.SUCCESS
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == "TX-1"


def test_second_push_after_success_does_not_call_client_again(settings):
    _arm(settings)
    assignment = _assignment()

    with patch.object(gnet_sync, "send_trip", return_value={"transactionId": "TX-1"}) as mock_send:
        first = gnet_sync.push_assignment(assignment)
        second = gnet_sync.push_assignment(assignment)

    assert mock_send.call_count == 1
    assert first.pk == second.pk
    assert second.result == GnetEvent.Result.SUCCESS


def test_deduped_response_is_treated_as_success(settings):
    _arm(settings)
    assignment = _assignment()

    with patch.object(
        gnet_sync, "send_trip", return_value={"transactionId": "TX-dedup", "deduped": True}
    ):
        event = gnet_sync.push_assignment(assignment)

    assert event.result == GnetEvent.Result.SUCCESS
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == "TX-dedup"


def test_second_push_after_error_does_not_resend(settings):
    """An ERROR must never be retried under the same requesterResNo (== assignment.pk,
    per build_send_payload). The correct way to retry a failed farm-out is a new
    Assignment row, not a second push of this one."""
    _arm(settings)
    assignment = _assignment()
    error = GnetAPIError(409, "conflict")

    with patch.object(gnet_sync, "send_trip", side_effect=error) as mock_send:
        first = gnet_sync.push_assignment(assignment)
        second = gnet_sync.push_assignment(assignment)

    assert mock_send.call_count == 1
    assert first.pk == second.pk
    assert second.result == GnetEvent.Result.ERROR


# --- a 2xx with no usable transactionId is a failure, not a success ---


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"reservationId": "R-1"},
        {"transactionId": ""},
        {"transactionId": "   "},
        {"deduped": True},
    ],
)
def test_2xx_without_usable_transaction_id_is_a_failure(settings, response):
    """A 2xx with no transactionId must never become a terminal SUCCESS: SUCCESS
    short-circuits every future push, and cancel_assignment no-ops on a blank id —
    so a false SUCCESS here would make the trip permanently uncancellable with a
    real vehicle possibly dispatched and nobody told."""
    _arm(settings)
    assignment = _assignment()

    with patch.object(gnet_sync, "send_trip", return_value=response):
        event = gnet_sync.push_assignment(assignment)

    assert event.result == GnetEvent.Result.ERROR
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_dedup_without_new_id_keeps_existing_transaction_id(settings):
    """The one legitimate exception: deduped:true with no transactionId in *this*
    response, but the assignment already has one stored from the original send —
    that original send having landed is still a success."""
    _arm(settings)
    assignment = _assignment(gnet_transaction_id="TX-original")

    with patch.object(gnet_sync, "send_trip", return_value={"deduped": True}):
        event = gnet_sync.push_assignment(assignment)

    assert event.result == GnetEvent.Result.SUCCESS
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == "TX-original"


# --- transaction.atomic() actually matters ---


def test_success_write_rolls_back_together_on_partial_failure(settings):
    """If the event's SUCCESS save fails after the assignment's id save already ran
    inside the same atomic block, both must roll back together — otherwise the
    assignment would carry a real transaction id while the event stays PENDING
    (not short-circuited), and a future push would resend under the same
    requesterResNo."""
    _arm(settings)
    assignment = _assignment()
    # Pre-create the PENDING event so get_or_create's lookup hits .get(), not
    # .save() — only the later explicit event.save() under test should be
    # intercepted by the patch below.
    idempotency_key = f"{gnet_sync.SEND_PREFIX}{assignment.pk}"
    GnetEvent.objects.create(
        assignment=assignment, action=GnetEvent.Action.SEND_TRIP, idempotency_key=idempotency_key
    )

    with patch.object(gnet_sync, "send_trip", return_value={"transactionId": "TX-1"}):
        with patch.object(GnetEvent, "save", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                gnet_sync.push_assignment(assignment)

    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""
    event = GnetEvent.objects.get(idempotency_key=idempotency_key)
    assert event.result == GnetEvent.Result.PENDING


# --- local refusal: never reaches the gateway ---


def test_gnet_not_configured_refusal_never_reaches_gateway(settings):
    _arm(settings)
    # No gnet_grid_id on the vendor -> build_send_payload raises GnetNotConfigured.
    assignment = _assignment(gnet_grid_id="")

    with patch.object(gnet_sync, "send_trip") as mock_send:
        event = gnet_sync.push_assignment(assignment)

    assert not mock_send.called
    assert event.result == GnetEvent.Result.ERROR
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


# --- gateway failures: alert, never retry ---


@pytest.mark.parametrize("status", [409, 422, 502, 503])
def test_api_error_records_error_alerts_and_is_never_retried(settings, status):
    _arm(settings)
    assignment = _assignment()
    error = GnetAPIError(status, "boom details")

    with patch.object(gnet_sync, "send_trip", side_effect=error) as mock_send:
        event = gnet_sync.push_assignment(assignment)

    assert mock_send.call_count == 1  # no retry loop inside a single push
    assert event.result == GnetEvent.Result.ERROR
    assert event.response == f"{status}: boom details"
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""


def test_api_error_notification_anchored_on_lead_and_truncated(settings):
    _arm(settings)
    assignment = _assignment()
    long_body = "x" * 3000
    error = GnetAPIError(422, long_body)

    with patch.object(gnet_sync, "send_trip", side_effect=error):
        event = gnet_sync.push_assignment(assignment)

    expected_response = f"422: {long_body}"[:2000]
    assert event.response == expected_response
    assert len(event.response) == 2000

    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert notification.lead_id == assignment.reservation.lead_id
    assert len(notification.title) <= 160
    assert len(notification.detail) <= 255


# --- transport failures (a RequestException, never a GnetAPIError) ---
#
# These deliberately do NOT mock `send_trip`/`cancel_trip` — that would bypass the
# very conversion in `gnet.py`'s `_request` this is regression-testing. A prior
# version of the GNet integration let `requests.exceptions.RequestException`
# (connection refused, timeout, ...) escape `_request` raw. That skipped
# `push_assignment`'s `except GnetAPIError` entirely, leaving the `GnetEvent` stuck
# PENDING (never ERROR), raising no alert, and — because PENDING isn't one of the
# two terminal results that short-circuit a re-push — leaving a second push free to
# resend under the exact same `requesterResNo`. For a `Timeout` that is exactly the
# double-booking risk Task 3 exists to prevent (see `test_gnet_client.py` for the
# lower-level client test of the same fix).


@pytest.mark.parametrize(
    "transport_error",
    [requests.exceptions.ConnectionError("refused"), requests.exceptions.Timeout("timed out")],
)
def test_transport_failure_is_recorded_as_error_and_alerts(settings, transport_error):
    _arm(settings)
    assignment = _assignment()

    with patch.object(gnet, "requests") as req:
        req.request.side_effect = transport_error
        event = gnet_sync.push_assignment(assignment)

    assert event.result == GnetEvent.Result.ERROR
    assignment.refresh_from_db()
    assert assignment.gnet_transaction_id == ""
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_second_push_after_transport_failure_makes_no_further_http_call(settings):
    _arm(settings)
    assignment = _assignment()

    with patch.object(gnet, "requests") as req:
        req.request.side_effect = requests.exceptions.ConnectionError("refused")
        first = gnet_sync.push_assignment(assignment)
        second = gnet_sync.push_assignment(assignment)

    assert req.request.call_count == 1  # ERROR is terminal — no silent resend
    assert first.pk == second.pk
    assert second.result == GnetEvent.Result.ERROR


def test_cancel_assignment_transport_failure_is_recorded_as_error_and_alerts(settings):
    _arm(settings)
    assignment = _assignment(gnet_transaction_id="TX-existing")

    with patch.object(gnet, "requests") as req:
        req.request.side_effect = requests.exceptions.Timeout("timed out")
        event = gnet_sync.cancel_assignment(assignment)

    assert event.result == GnetEvent.Result.ERROR
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


# --- cancel_assignment ---


def test_cancel_assignment_noop_without_transaction_id():
    assignment = _assignment()
    assert assignment.gnet_transaction_id == ""

    with patch.object(gnet_sync, "cancel_trip") as mock_cancel:
        result = gnet_sync.cancel_assignment(assignment)

    assert result is None
    assert not mock_cancel.called
    assert not GnetEvent.objects.filter(
        assignment=assignment, action=GnetEvent.Action.CANCEL_TRIP
    ).exists()


def test_cancel_assignment_preview_mode_no_http_call(settings):
    settings.GNET_ACTIVE = False
    settings.GNET_API_KEY = "lds_testkey1234567890"
    assignment = _assignment(gnet_transaction_id="TX-existing")

    with patch.object(gnet_sync, "cancel_trip") as mock_cancel:
        event = gnet_sync.cancel_assignment(assignment)

    assert not mock_cancel.called
    assert event.result == GnetEvent.Result.PREVIEW


def test_cancel_assignment_success_marks_event_success(settings):
    _arm(settings)
    assignment = _assignment(gnet_transaction_id="TX-existing")

    with patch.object(
        gnet_sync, "cancel_trip", return_value={"status": "cancelled"}
    ) as mock_cancel:
        event = gnet_sync.cancel_assignment(assignment)

    assert mock_cancel.called
    assert event.result == GnetEvent.Result.SUCCESS


def test_cancel_assignment_api_error_records_error_and_alerts(settings):
    _arm(settings)
    assignment = _assignment(gnet_transaction_id="TX-existing")
    error = GnetAPIError(502, "gateway hiccup")

    with patch.object(gnet_sync, "cancel_trip", side_effect=error):
        event = gnet_sync.cancel_assignment(assignment)

    assert event.result == GnetEvent.Result.ERROR
    assert event.response == "502: gateway hiccup"
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_a_failed_cancel_alert_carries_the_transaction_id(settings):
    """`reservation_delete` calls `release_trips` and then deletes the reservation,
    CASCADEing the assignment and its GnetEvents. If the cancel had just failed — a
    502/503, exactly when failures cluster — the alert was the only surviving record
    and it named no transaction id, leaving the GNet booking unreconcilable from APC.
    The id goes ahead of the gateway body so the 255-char truncation can't eat it."""
    _arm(settings)
    assignment = _assignment(gnet_transaction_id="TX-existing")

    with patch.object(gnet_sync, "cancel_trip", side_effect=GnetAPIError(503, "x" * 3000)):
        gnet_sync.cancel_assignment(assignment)

    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "TX-existing" in notification.detail
