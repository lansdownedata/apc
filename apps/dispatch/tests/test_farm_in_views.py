from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.dispatch import farm_in, farm_in_actions, farm_in_push
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.dispatch.tests.farm_in_payload import sample_payload
from apps.leads.factories import VehicleTypeFactory
from apps.leads.models import Lead, VehicleType

pytestmark = pytest.mark.django_db
TID = "935a1056-644c-4e82-9a1a-6099efdd056b"


@pytest.fixture
def sedan():
    vt = VehicleTypeFactory(name="Luxury Sedan")
    VehicleType.objects.filter(pk=vt.pk).update(rate=Decimal("165"), transfer_min_hours=1)
    vt.refresh_from_db()
    return vt


def _url(name: str, pk: int) -> str:
    return reverse(name, args=[pk])


def test_price_endpoint_updates_and_returns_amount(logged_in_client):
    req = FarmInRequestFactory()
    resp = logged_in_client.post(_url("farm_in_price", req.pk), {"amount": "165.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["quoted_amount"] == "165.00"
    assert body["status"] == req.status
    assert body["desk"] == req.desk


def test_price_endpoint_rejects_garbage_amount(logged_in_client):
    req = FarmInRequestFactory()
    resp = logged_in_client.post(_url("farm_in_price", req.pk), {"amount": "abc"})
    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "Enter a valid amount"}


def test_price_endpoint_unknown_pk_is_404(logged_in_client):
    resp = logged_in_client.post(_url("farm_in_price", 999999), {"amount": "10"})
    assert resp.status_code == 404


def test_vehicle_endpoint(logged_in_client, sedan):
    req = FarmInRequestFactory()
    resp = logged_in_client.post(_url("farm_in_vehicle", req.pk), {"vehicle_id": sedan.pk})
    assert resp.status_code == 200
    req.refresh_from_db()
    assert req.vehicle_id == sedan.pk
    bad = logged_in_client.post(_url("farm_in_vehicle", req.pk), {"vehicle_id": "x"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "Choose a vehicle"


def test_request_coverage_and_send_back_endpoints(logged_in_client):
    req = FarmInRequestFactory()
    cov = logged_in_client.post(_url("farm_in_request_coverage", req.pk))
    assert cov.status_code == 200
    assert cov.json()["ok"] is True
    back = logged_in_client.post(_url("farm_in_send_back", req.pk), {"note": "Need a flight"})
    assert back.status_code == 200
    assert back.json()["ok"] is True


def test_decline_endpoint_returns_409_when_already_accepted(logged_in_client):
    req = FarmInRequestFactory(status=FarmInRequest.Status.ACCEPTED)
    resp = logged_in_client.post(_url("farm_in_decline", req.pk), {"reason": "No cars"})
    assert resp.status_code == 409
    assert resp.json()["ok"] is False


def test_accept_endpoint_books(
    logged_in_client, sedan, settings, django_capture_on_commit_callbacks
):
    settings.GNET_API_KEY = "k"
    AirportFactory(iata="LAX", name="Los Angeles International", timezone="America/Los_Angeles")
    AirlineFactory(iata="UA")
    farm_in.handle_new(sample_payload(), transaction_id=TID, delivery_id="d-1")
    req = FarmInRequest.objects.get(transaction_id=TID)
    logged_in_client.post(_url("farm_in_price", req.pk), {"amount": "165.00"})
    with (
        patch.object(farm_in_push, "deliver"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        resp = logged_in_client.post(_url("farm_in_accept", req.pk))
    assert resp.status_code == 200
    assert resp.json()["status"] == FarmInRequest.Status.ACCEPTED


def test_a_missing_row_inside_an_action_is_not_a_404(logged_in_client):
    req = FarmInRequestFactory()
    with patch.object(farm_in_actions, "accept", side_effect=Lead.DoesNotExist("bug")):
        with pytest.raises(Lead.DoesNotExist):
            logged_in_client.post(_url("farm_in_accept", req.pk))


def test_retry_push_unknown_pk_is_404(logged_in_client):
    resp = logged_in_client.post(reverse("farm_in_retry_push", args=[999999]))
    assert resp.status_code == 404


def test_retry_push_endpoint(logged_in_client):
    push = FarmInStatusPushFactory(state=FarmInStatusPush.State.FAILED)
    resp = logged_in_client.post(reverse("farm_in_retry_push", args=[push.pk]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["push"] != push.pk


@pytest.mark.parametrize(
    "name",
    [
        "farm_in_price",
        "farm_in_vehicle",
        "farm_in_request_coverage",
        "farm_in_send_back",
        "farm_in_accept",
        "farm_in_decline",
        "farm_in_retry_push",
    ],
)
def test_endpoints_require_login(client, name):
    resp = client.post(reverse(name, args=[1]))
    assert resp.status_code == 302
    assert "login" in resp.url


@pytest.mark.parametrize(
    "name",
    [
        "farm_in_price",
        "farm_in_vehicle",
        "farm_in_request_coverage",
        "farm_in_send_back",
        "farm_in_accept",
        "farm_in_decline",
        "farm_in_retry_push",
    ],
)
def test_endpoints_require_post(logged_in_client, name):
    assert logged_in_client.get(reverse(name, args=[1])).status_code == 405
