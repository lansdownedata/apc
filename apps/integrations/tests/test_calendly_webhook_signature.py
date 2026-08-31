"""Calendly webhook HMAC verification: sha256 over '{t}.{raw_body}'.

Calendly sends `Calendly-Webhook-Signature: t=<unix seconds>,v1=<hex digest>`, keyed
with the signing_key supplied when the subscription was created. Confirmed against
Calendly's webhook-signatures docs on 2026-08-31 — the signed payload is the
timestamp, a literal '.', and the RAW request body.
"""

import hashlib
import hmac
import json

import pytest

pytestmark = pytest.mark.django_db

URL = "/webhooks/calendly/"
BODY = json.dumps({"event": "invitee.created", "payload": {}}).encode()


def _sig(key: str, ts: str, body: bytes) -> str:
    return hmac.new(key.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _post(client, body=BODY, **headers):
    return client.post(URL, data=body, content_type="application/json", **headers)


def test_valid_signature_accepted(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    header = f"t=1757353800,v1={_sig('cwk', '1757353800', BODY)}"
    assert _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE=header).status_code == 200


def test_wrong_signature_403(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    assert _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE="t=1,v1=deadbeef").status_code == 403


def test_missing_header_403(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    assert _post(client).status_code == 403


def test_malformed_header_403(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    assert _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE="garbage").status_code == 403


def test_tampered_body_403(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    header = f"t=1757353800,v1={_sig('cwk', '1757353800', BODY)}"
    evil = json.dumps({"event": "invitee.created", "payload": {"evil": 1}}).encode()
    assert _post(client, body=evil, HTTP_CALENDLY_WEBHOOK_SIGNATURE=header).status_code == 403


def test_timestamp_is_part_of_the_signed_payload(client, settings):
    """A signature valid for one timestamp must not be replayable under another."""
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    header = f"t=1757353801,v1={_sig('cwk', '1757353800', BODY)}"
    assert _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE=header).status_code == 403


def test_blank_key_accepts_unsigned(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = ""
    assert _post(client).status_code == 200


def test_non_ascii_signature_header_is_rejected_not_a_500(client, settings):
    """compare_digest raises TypeError on non-ASCII str — a 500 on a public endpoint."""
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    assert _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE="t=1,v1=déadbeef").status_code == 403


def test_get_is_rejected(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = ""
    assert client.get(URL).status_code == 400


def test_invalid_json_is_rejected(client, settings):
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = ""
    assert client.post(URL, data=b"{oops", content_type="application/json").status_code == 400


def test_valid_json_that_is_not_an_object_is_answered_200(client, settings):
    """A list parses fine but has no .get — reject before it reaches processing, and
    answer 200 so Calendly doesn't retry a request that can never succeed."""
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = ""
    assert client.post(URL, data=b"[1,2]", content_type="application/json").status_code == 200


def test_failed_verification_logs_the_header_it_saw(client, settings, caplog):
    """The header format is the one thing we cannot test against real traffic until
    real traffic arrives. A bare 403 with no record of what was sent makes the first
    live delivery unusable as evidence."""
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    with caplog.at_level("WARNING"):
        _post(client, HTTP_CALENDLY_WEBHOOK_SIGNATURE="t=1,v1=deadbeef")
    assert "t=1,v1=deadbeef" in caplog.text
