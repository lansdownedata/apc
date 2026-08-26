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
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.dispatch import gnet_callback
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.gnet_callback import handle_callback
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


def test_an_affiliate_cancel_does_not_echo_a_delete_back_to_the_gateway(client, settings):
    """The affiliate already cancelled. Echoing a DELETE earns a rejection plus a
    spurious "GNet cancel failed" alert next to the correct one, on every single
    affiliate cancellation — and puts a 10s-timeout outbound call inside the gateway's
    15s callback budget."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(status=Assignment.Status.CONFIRMED)

    with patch.object(gnet_callback.services.gnet_sync, "cancel_assignment") as mock_cancel:
        resp = _signed_post(client, {"transactionId": "TX-1", "status": "CANCEL"})

    assert resp.status_code == 200
    assert not mock_cancel.called
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.WITHDRAWN


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


# --- valid JSON that isn't an object ---
#
# json.loads happily returns a list, string, number, bool, or None for
# well-formed-but-non-object JSON — none of those raise JSONDecodeError, so the
# view's except-JSONDecodeError guard alone lets them through to handle_callback,
# which used to call payload.get(...) unconditionally and crash. A crash here is
# the worst possible failure shape: the gateway retries a non-2xx 3x plus a
# sweeper, so one bad delivery turns into a storm of identical crashes.


@pytest.mark.parametrize(
    "body_obj",
    [[1, 2, 3], [], "just a string", 12345, True, None],
    ids=["array", "empty-array", "string", "number", "bool", "null"],
)
def test_non_object_json_body_does_not_500(client, settings, body_obj):
    settings.GNET_CALLBACK_SECRET = SECRET

    resp = _signed_post(client, body_obj)

    assert resp.status_code < 500
    assert not GnetEvent.objects.exists()


def test_valid_object_body_still_works(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "CONFIRMED"})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_handle_callback_never_raises_on_non_dict_payload():
    """The view is the primary gate, but handle_callback's own docstring promises
    it never raises — that must hold even when called directly with a non-dict."""
    for bad_payload in ([1, 2, 3], "oops", None, 12345, True, []):
        event = handle_callback(bad_payload)
        assert isinstance(event, GnetEvent)


# --- "absent" vs "falsy" ---


def test_falsy_but_present_status_is_not_treated_as_absent():
    """A bare `or ""` would silently treat a present-but-falsy status (0, False)
    as missing. handle_callback must distinguish "absent" from "falsy"."""
    event = handle_callback({"transactionId": "TX-falsy-status", "status": 0})
    assert event.idempotency_key == "callback-TX-falsy-status-0"


# --- the dedupe row must not outlive a failed state change ---
#
# There is no ATOMIC_REQUESTS, so `get_or_create` commits the dedupe row on its own. If
# the state change then raised, the view 500'd and every one of the gateway's retries
# found that row, returned 200, and never applied the status — the assignment sat
# OFFERED forever while the affiliate was confirmed.


def test_a_failed_state_change_rolls_back_the_dedupe_row():
    assignment = _gnet_assignment()

    with (
        patch.object(gnet_callback.services, "confirm", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
    ):
        handle_callback({"transactionId": "TX-1", "status": "CONFIRMED"})

    assert not GnetEvent.objects.filter(idempotency_key="callback-TX-1-CONFIRMED").exists()

    # The gateway's retry now actually re-applies rather than hitting a stale row.
    handle_callback({"transactionId": "TX-1", "status": "CONFIRMED"})
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_an_oversized_transaction_id_does_not_overflow_the_dedupe_key(client, settings):
    """`idempotency_key` is CharField(max_length=160); a partner id longer than that
    used to raise DataError on the inbound hot path."""
    settings.GNET_CALLBACK_SECRET = SECRET

    resp = _signed_post(client, {"transactionId": "X" * 400, "status": "Y" * 400})

    assert resp.status_code == 200
    event = GnetEvent.objects.get()
    assert len(event.idempotency_key) <= 160


# --- header comparison must never raise on non-ASCII (an unauthenticated 500 otherwise) ---


def test_non_ascii_authorization_header_is_rejected_not_a_500(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()

    resp = _post(
        client,
        body,
        HTTP_AUTHORIZATION="Bearer ünïcode",
        HTTP_X_LANSDOWNE_SIGNATURE=_sig(SECRET, body),
    )

    assert resp.status_code == 403


def test_non_ascii_signature_header_is_rejected_not_a_500(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    body = json.dumps({"transactionId": "TX-1", "status": "CONFIRMED"}).encode()

    resp = _post(
        client,
        body,
        HTTP_AUTHORIZATION=f"Bearer {SECRET}",
        HTTP_X_LANSDOWNE_SIGNATURE="sha256=déadbeef",
    )

    assert resp.status_code == 403


# --- payout bounds: the callback writes the same field the dispatcher door guards ---


@pytest.mark.parametrize(
    "amount",
    [
        "-500.00",
        "-0.01",
        "99999999.995",
        "100000000.00",
        "99999999999.99",
        "1e999",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_out_of_range_totalamount_is_treated_as_absent(client, settings, amount):
    """`views._payout` refuses negatives and anything >= 99999999.995; the callback
    writes the same MoneyField and must refuse the same values — as "absent," never as
    an exception on a public endpoint."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(client, {"transactionId": "TX-1", "status": "CLOSE", "totalAmount": amount})

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("140.00")


def test_totalamount_is_quantized_to_cents(client, settings):
    """MySQL rounds a third decimal half-even and Postgres half-up — quantize here so
    both environments store the same money, exactly as `views._payout` does."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    _signed_post(client, {"transactionId": "TX-1", "status": "CLOSE", "totalAmount": "215.005"})

    assignment.refresh_from_db()
    assert assignment.payout == Decimal("215.01")


def test_a_pre_close_reprice_that_moves_payout_alerts(client, settings):
    """A provisional first quote can flip a trip's margin from positive to negative.
    Auto-healing is right; doing it silently is not."""
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("140.00"))

    resp = _signed_post(
        client, {"transactionId": "TX-1", "status": "ASSIGNED", "totalAmount": "150.00"}
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.payout == Decimal("150.00")
    notification = Notification.objects.get(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    )
    assert "150.00" in notification.detail


def test_a_reprice_that_does_not_move_payout_stays_silent(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment(payout=Decimal("150.00"))

    _signed_post(client, {"transactionId": "TX-1", "status": "ASSIGNED", "totalAmount": "150.00"})

    assert not Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


# --- correlation by requesterResNo when the body carries no transactionId ---
#
# The gateway's own callbackSchema allows a body with no transactionId when
# affiliateReservation.requesterResNo is present, and its correlate() implements that
# fallback. Correlating on transactionId alone left every such callback unapplied — and,
# worse, collided them all on one `callback--<STATUS>` dedupe key, so the second one
# recorded nothing at all, losing even the audit evidence.


def test_callback_without_transaction_id_correlates_by_requester_res_no(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    assignment = _gnet_assignment()

    resp = _signed_post(
        client,
        {
            "status": "CONFIRMED",
            "affiliateReservation": {"requesterResNo": f"apc-{assignment.pk}"},
        },
    )

    assert resp.status_code == 200
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_id_less_callbacks_for_different_assignments_do_not_collide(client, settings):
    settings.GNET_CALLBACK_SECRET = SECRET
    first = _gnet_assignment()
    second = _gnet_assignment(gnet_transaction_id="TX-2")

    for assignment in (first, second):
        _signed_post(
            client,
            {
                "status": "CONFIRMED",
                "affiliateReservation": {"requesterResNo": f"apc-{assignment.pk}"},
            },
        )

    assert GnetEvent.objects.filter(action=GnetEvent.Action.CALLBACK).count() == 2
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == Assignment.Status.CONFIRMED
    assert second.status == Assignment.Status.CONFIRMED


@pytest.mark.parametrize(
    "res_no",
    ["not-a-number", "apc-", "", None, "apc-99999999999999999999999", {"a": 1}],
    ids=["text", "prefix-only", "blank", "null", "out-of-range", "object"],
)
def test_an_unusable_requester_res_no_is_recorded_and_never_crashes(client, settings, res_no):
    settings.GNET_CALLBACK_SECRET = SECRET

    resp = _signed_post(
        client,
        {"status": "CONFIRMED", "affiliateReservation": {"requesterResNo": res_no}},
    )

    assert resp.status_code == 200
    assert GnetEvent.objects.get().assignment is None


def test_transaction_id_still_wins_when_both_are_present(client, settings):
    """The id is the gateway's own correlator; the resNo is only a fallback."""
    settings.GNET_CALLBACK_SECRET = SECRET
    by_id = _gnet_assignment()
    by_res_no = _gnet_assignment(gnet_transaction_id="TX-2")

    _signed_post(
        client,
        {
            "transactionId": "TX-1",
            "status": "CONFIRMED",
            "affiliateReservation": {"requesterResNo": f"apc-{by_res_no.pk}"},
        },
    )

    by_id.refresh_from_db()
    by_res_no.refresh_from_db()
    assert by_id.status == Assignment.Status.CONFIRMED
    assert by_res_no.status == Assignment.Status.OFFERED
