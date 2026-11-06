import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.dispatch.models import FarmInRequest
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import VehicleType

pytestmark = pytest.mark.django_db
SECRET = "farminsecret"
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client, payload, *, secret=SECRET, event="farm_in.new", tid=TID, sig=None, bearer=None):
    body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return client.post(
        reverse("gnet_farm_in"),
        data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION=bearer or f"Bearer {secret}",
        HTTP_X_LANSDOWNE_SIGNATURE=sig or _sig(secret, body),
        HTTP_X_LANSDOWNE_EVENT=event,
        HTTP_X_LANSDOWNE_TRANSACTION_ID=tid,
        HTTP_X_LANSDOWNE_DELIVERY_ID="d-1",
    )


def _priced_sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan", rate=Decimal("165"))
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    return vt


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.GNET_CALLBACK_SECRET = SECRET


def test_get_probe_needs_nothing(client, settings):
    settings.GNET_CALLBACK_SECRET = ""
    r = client.get(reverse("gnet_farm_in"))
    assert r.status_code == 200 and r.json() == {"success": True}


def test_blank_secret_refuses(client, settings):
    settings.GNET_CALLBACK_SECRET = ""
    r = _post(client, sample_payload(), secret="x")
    assert r.status_code == 200 and r.json() == {"error": "GNet farm-in is not configured"}
    assert FarmInRequest.objects.count() == 0


def test_bad_bearer_403(client):
    assert _post(client, sample_payload(), bearer="Bearer wrong").status_code == 403


def test_bad_signature_403(client):
    assert _post(client, sample_payload(), sig="sha256=deadbeef").status_code == 403


def test_non_object_body(client):
    r = _post(client, b"[1,2]")
    assert r.status_code == 200 and r.json() == {"error": "malformed payload"}


def test_invalid_json(client):
    r = _post(client, b"{not json")
    assert r.status_code == 200 and r.json() == {"error": "malformed payload"}


def test_unknown_event(client):
    r = _post(client, sample_payload(), event="farm_in.what")
    assert r.json() == {"error": "unsupported event farm_in.what"}


def test_new_end_to_end(client):
    _priced_sedan()
    r = _post(client, sample_payload())
    req = FarmInRequest.objects.get(transaction_id=TID)
    assert r.json() == {"reservationId": f"FI-{req.pk}", "totalAmount": "165.00"}
    assert req.delivery_id == "d-1"


def test_header_transaction_id_wins_over_body(client):
    _post(client, sample_payload(), tid="header-tid")
    assert FarmInRequest.objects.get().transaction_id == "header-tid"


def test_quote_end_to_end(client):
    _priced_sedan()
    r = _post(client, sample_payload(), event="farm_in.quote")
    assert r.json() == {"reservationId": TID, "totalAmount": "165.00"}
    assert FarmInRequest.objects.count() == 0


def test_service_exception_becomes_internal_error(client):
    with patch("apps.integrations.views.farm_in.receive", side_effect=RuntimeError("boom")):
        r = _post(client, sample_payload())
    assert r.status_code == 200 and r.json() == {"error": "internal error"}


def test_notification_failure_does_not_change_response(client, django_capture_on_commit_callbacks):
    _priced_sedan()
    with patch(
        "apps.notifications.models.Notification.notify", side_effect=RuntimeError("bell down")
    ):
        with django_capture_on_commit_callbacks(execute=True):
            r = _post(client, sample_payload())
    assert "reservationId" in r.json() and FarmInRequest.objects.count() == 1


def test_put_not_allowed(client):
    assert client.put(reverse("gnet_farm_in")).status_code == 405


def test_non_utf8_body(client):
    r = _post(client, b"\xff\xfe\x00")
    assert r.status_code == 200 and r.json() == {"error": "malformed payload"}


def test_empty_tid_header_falls_back_to_body(client):
    payload = sample_payload()
    _post(client, payload, tid="")
    assert FarmInRequest.objects.get().transaction_id == payload["transactionId"]
