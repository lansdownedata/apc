"""GNet farm-out callback receiver (GNET-CONNECTION-GUIDE.md §5.8): signature check,
status mapping onto `dispatch.services`, dedupe, and payout auto-heal.

Mirrors test_podium_webhook_signature.py's HMAC pattern. No test performs real
network I/O: GNET_ACTIVE/GNET_API_KEY are left at their base-settings defaults
(both off), so any `services.withdraw()` call that reaches
`gnet_sync.cancel_assignment` runs in preview mode and never touches `requests`.
"""

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, GnetEvent
from apps.notifications.models import Notification
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db

SECRET = "gnetsecret"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, body: bytes, **headers):
    return client.post(
        reverse("gnet_callback"), data=body, content_type="application/json", **headers
    )


def _signed_post(client, payload: dict, secret: str = SECRET):
    body = json.dumps(payload).encode()
    return _post(
        client,
        body,
        HTTP_AUTHORIZATION=f"Bearer {secret}",
        HTTP_X_LANSDOWNE_SIGNATURE=_sig(secret, body),
    )


def _gnet_assignment(**kwargs):
    """A GNet-channel assignment already sent to the gateway (has a transaction id) —
    the shape every real callback in this file correlates against."""
    kwargs.setdefault("vendor", VendorFactory(gnet_grid_id="gnet-partner-1"))
    kwargs.setdefault("channel", Assignment.Channel.GNET)
    kwargs.setdefault("gnet_transaction_id", "TX-1")
    kwargs.setdefault("status", Assignment.Status.OFFERED)
    kwargs.setdefault("payout", Decimal("140.00"))
    return AssignmentFactory(**kwargs)


# --- signature verification ---


def test_bad_signature_403(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()
    resp = _post(
        client,
        body,
        HTTP_AUTHORIZATION=f"Bearer {SECRET}",
        HTTP_X_LANSDOWNE_SIGNATURE="sha256=deadbeef",
    )
    assert resp.status_code == 403


def test_missing_signature_403_when_secret_set(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()
    resp = _post(client, body, HTTP_AUTHORIZATION=f"Bearer {SECRET}")
    assert resp.status_code == 403


def test_blank_secret_accepts_unsigned(client, settings):
    settings.GNET_CALLBACK_SECRET = ""
    body = json.dumps({"transactionId": "unknown-tx", "status": "CONFIRMED"}).encode()
    resp = _post(client, body)
    assert resp.status_code == 200


# --- Authorization: Bearer verification (defence in depth alongside the HMAC) ---


def test_missing_authorization_header_403(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()
    resp = _post(client, body, HTTP_X_LANSDOWNE_SIGNATURE=_sig(SECRET, body))
    assert resp.status_code == 403


def test_wrong_authorization_bearer_403(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()
    resp = _post(
        client,
        body,
        HTTP_AUTHORIZATION="Bearer wrong-secret",
        HTTP_X_LANSDOWNE_SIGNATURE=_sig(SECRET, body),
    )
    assert resp.status_code == 403


def test_correct_authorization_bearer_accepted(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    resp = _signed_post(client, {"transactionId": "unknown-tx", "status": "CONFIRMED"})
    assert resp.status_code == 200


# --- status mapping ---


def test_confirmed_confirms_assignment(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "CONFIRMED"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_reject_declines_assignment(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(
        client,
        {
            "transactionId": "TX-1",
            "status": "REJECT",
            "affiliateReservation": {"notes": "no vehicle available"},
        },
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.DECLINED


def test_cancel_withdraws_and_alerts(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(status=Assignment.Status.CONFIRMED)

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "CANCEL"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.WITHDRAWN
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_failed_leaves_offered_and_alerts(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(
        client,
        {
            "transactionId": "TX-1",
            "status": "FAILED",
            "affiliateReservation": {"notes": "vehicle broke down"},
        },
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.OFFERED
    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "vehicle broke down" in notification.detail


def test_failed_notes_as_bare_string_parses(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    _signed_post(
        client,
        {
            "transactionId": "TX-1",
            "status": "FAILED",
            "affiliateReservation": {"notes": "no driver on duty"},
        },
    )

    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "no driver on duty" in notification.detail


def test_failed_notes_as_list_of_objects_parses(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    _signed_post(
        client,
        {
            "transactionId": "TX-1",
            "status": "FAILED",
            "affiliateReservation": {
                "notes": [
                    {"message": "no driver available", "context": "dispatch"},
                    {"message": "try again later", "context": "dispatch"},
                ]
            },
        },
    )

    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "no driver available" in notification.detail


def test_unknown_status_records_event_and_changes_nothing(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "EN_ROUTE"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.OFFERED
    assert GnetEvent.objects.filter(idempotency_key="callback-TX-1-EN_ROUTE").exists()


def test_completely_unrecognised_status_does_not_500(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    _gnet_assignment()

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "SOME_NEW_PARTNER_STATUS"})

    assert resp.status_code == 200


def test_already_resolved_assignment_yields_200_not_exception(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(status=Assignment.Status.WITHDRAWN)

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "CONFIRMED"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.WITHDRAWN


# --- dedupe ---


def test_repeat_delivery_is_a_noop(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()
    payload = {"transactionId": "TX-1", "status": "CONFIRMED"}

    first = _signed_post(client, payload)
    second = _signed_post(client, payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert GnetEvent.objects.filter(idempotency_key="callback-TX-1-CONFIRMED").count() == 1
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_unknown_transaction_id_returns_200_and_records_uncorrelated_event(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET

    resp = _signed_post(client, {"transactionId": "TX-does-not-exist", "status": "CONFIRMED"})

    assert resp.status_code == 200
    event = GnetEvent.objects.get(idempotency_key="callback-TX-does-not-exist-CONFIRMED")
    assert event.assignment is None


# --- payout auto-heal ---


def test_totalamount_updates_payout_silently(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "ASSIGNED", "totalAmount": "150.00"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("150.00")
    assert not Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_later_totalamount_supersedes_earlier(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    _signed_post(client, {"transactionId": "TX-1", "status": "ASSIGNED", "totalAmount": "150.00"})
    _signed_post(client, {"transactionId": "TX-1", "status": "CLOSE", "totalAmount": "156.75"})

    assignment.refresh_from_db()
    assert assignment.payout == Decimal("156.75")


def test_close_amount_differing_from_recorded_payout_alerts(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "CLOSE", "totalAmount": "156.75"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("156.75")
    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "156.75" in notification.detail


def test_close_amount_matching_recorded_payout_is_silent(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("156.75"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "CLOSE", "totalAmount": "156.75"}
    )

    assert resp.status_code == 200
    assert not Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_float_totalamount_treated_as_absent(client, settings):
    """`totalAmount` is a string in this contract; a JSON number means absent —
    parsing it must never crash the receiver."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "CONFIRMED", "totalAmount": 150.0}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("140.00")


def test_unparseable_totalamount_string_does_not_crash(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "CONFIRMED", "totalAmount": "not-a-number"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("140.00")


def test_reject_with_totalamount_leaves_payout_untouched(client, settings):
    """REJECT means the affiliate declined — nobody is covering the trip, so an
    amount in the payload is not a payout owed and must not be written."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "REJECT", "totalAmount": "999.00"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("140.00")
    assert assignment.status == Assignment.Status.DECLINED


def test_failed_with_totalamount_leaves_payout_untouched(client, settings):
    """FAILED leaves the assignment OFFERED — a still-open offer must never pick up
    a price from a failure message."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "FAILED", "totalAmount": "999.00"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("140.00")
    assert assignment.status == Assignment.Status.OFFERED
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_cancel_with_totalamount_still_auto_heals(client, settings):
    """CANCEL happens after acceptance, so an amount there is a plausible
    cancellation charge — auto-heal stays on for this status."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(status=Assignment.Status.CONFIRMED, payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "CANCEL", "totalAmount": "160.00"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("160.00")
    assert assignment.status == Assignment.Status.WITHDRAWN
