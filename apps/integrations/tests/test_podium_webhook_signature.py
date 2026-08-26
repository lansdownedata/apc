"""Podium webhook HMAC verification: sha256 over '{timestamp}.{raw_body}'."""

import hashlib
import hmac
import json

import pytest

pytestmark = pytest.mark.django_db

URL = "/webhooks/podium/"
BODY = json.dumps({"eventType": "message.received", "data": {}}).encode()


def _sig(secret: str, ts: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def _post(client, body=BODY, **headers):
    return client.post(URL, data=body, content_type="application/json", **headers)


def test_valid_signature_accepted(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = "whsec"
    resp = _post(
        client,
        HTTP_PODIUM_TIMESTAMP="1620771659663",
        HTTP_PODIUM_SIGNATURE=_sig("whsec", "1620771659663", BODY),
    )
    assert resp.status_code == 200


def test_wrong_signature_403(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = "whsec"
    resp = _post(client, HTTP_PODIUM_TIMESTAMP="1", HTTP_PODIUM_SIGNATURE="deadbeef")
    assert resp.status_code == 403


def test_missing_headers_403(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = "whsec"
    assert _post(client).status_code == 403


def test_tampered_body_403(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = "whsec"
    resp = _post(
        client,
        body=json.dumps({"eventType": "message.received", "data": {"evil": 1}}).encode(),
        HTTP_PODIUM_TIMESTAMP="1620771659663",
        HTTP_PODIUM_SIGNATURE=_sig("whsec", "1620771659663", BODY),
    )
    assert resp.status_code == 403


def test_blank_secret_accepts_unsigned(client, settings):
    settings.PODIUM_WEBHOOK_SECRET = ""
    assert _post(client).status_code == 200


def test_non_ascii_signature_header_is_rejected_not_a_500(client, settings):
    """`hmac.compare_digest` raises TypeError when either str argument is non-ASCII —
    an unauthenticated 500 on a public endpoint. Compare bytes instead."""
    settings.PODIUM_WEBHOOK_SECRET = "whsec"
    resp = _post(client, HTTP_PODIUM_TIMESTAMP="1", HTTP_PODIUM_SIGNATURE="déadbeef")
    assert resp.status_code == 403
