"""GNet farm-out gateway client — HTTP request/error plumbing (requests fully mocked).

SAFETY: the gateway is deployed in production and talks to real GNet (api.grdd.net) — a
successful send books a REAL vehicle with a REAL affiliate. Every test here mocks
`requests` at the boundary via `patch.object(gnet, "requests")`; none may perform real
network I/O, and no real API key is ever used (a fake `lds_...`-shaped string only).
"""

import json
from datetime import date, time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from apps.dispatch.factories import AssignmentFactory
from apps.integrations import gnet
from apps.leads.factories import VehicleTypeFactory
from apps.reservations.factories import ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def gnet_settings(settings):
    settings.GNET_GATEWAY_URL = "https://gateway.example.test"
    settings.GNET_API_KEY = "lds_testkey1234567890"


def _response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.content = b"x" if json_data is not None else b""
    return resp


def _assignment(vehicle_name="Luxury Sedan", **kwargs):
    vendor = VendorFactory(gnet_grid_id="gnet-partner-42")
    vehicle = VehicleTypeFactory(name=vehicle_name)
    reservation = ReservationFactory(
        vehicle=vehicle,
        passengers=3,
        rate=Decimal("500.00"),
        hours=Decimal("3"),
        pickup_date=date(2026, 9, 1),
        pickup_time=time(14, 0),
        stops=["1600 Pennsylvania Ave NW", "IAD Airport"],
    )
    return AssignmentFactory(
        reservation=reservation, vendor=vendor, payout=Decimal("300.00"), **kwargs
    )


# --- vehicle_code ---


def test_vehicle_code_maps_known_vehicle():
    vehicle = VehicleTypeFactory(name="Luxury Sedan")
    assert gnet.vehicle_code(vehicle) == "SEDAN_LUX"


def test_vehicle_code_maps_all_six_seeded_names():
    for name, code in gnet.VEHICLE_TYPE_MAP.items():
        vehicle = VehicleTypeFactory(name=name)
        assert gnet.vehicle_code(vehicle) == code


def test_vehicle_code_raises_for_unmapped_vehicle():
    vehicle = VehicleTypeFactory(name="Party Bus")
    with pytest.raises(gnet.GnetNotConfigured):
        gnet.vehicle_code(vehicle)


# --- build_send_payload ---


def test_payload_has_requester_res_no_and_provider_id():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["affiliateReservation"]["requesterResNo"] == str(assignment.pk)
    assert payload["affiliateReservation"]["providerId"] == assignment.vendor.gnet_grid_id


def test_payload_never_sends_injected_fields():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert "transactionId" not in payload
    assert "requesterId" not in payload["affiliateReservation"]


def test_payload_uses_mapped_vehicle_code():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["preferredVehicleType"] == "SEDAN_LUX"


def test_payload_raises_for_unmapped_vehicle():
    assignment = _assignment(vehicle_name="Party Bus")
    with pytest.raises(gnet.GnetNotConfigured):
        gnet.build_send_payload(assignment)


def test_payload_passenger_count_and_naive_pickup_time():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["passengerCount"] == "3"
    pickup_time = payload["locations"]["pickup"]["time"]
    # naive local time, verbatim — never invent a timezone conversion/suffix
    assert pickup_time == "2026-09-01T14:00:00"
    assert "+" not in pickup_time
    assert "Z" not in pickup_time


def test_payload_stops_map_to_pickup_and_dropoff_addresses():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["locations"]["pickup"]["address"] == "1600 Pennsylvania Ave NW"
    assert payload["locations"]["dropOff"]["address"] == "IAD Airport"


def test_payload_money_fields_are_strings():
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    payout_value = payload["affiliateReservation"]["payoutAmount"]
    assert isinstance(payout_value, str)
    assert payout_value == "300.00"


def test_payload_never_leaks_customer_price():
    """Brokerage margin is confidential — only the payout goes to the affiliate."""
    assignment = _assignment()
    reservation = assignment.reservation
    assert reservation.line_total == Decimal("1500.00")  # rate 500 x 3h, no gratuity
    payload = gnet.build_send_payload(assignment)
    serialized = json.dumps(payload)
    assert "1500.00" not in serialized
    assert str(reservation.line_total) not in serialized


# --- send_trip / cancel_trip ---


def test_send_trip_sets_authorization_header_and_timeout():
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(
            json_data={"transactionId": "t-1", "reservationId": "R1", "totalAmount": "142.50"}
        )
        gnet.send_trip({"affiliateReservation": {"requesterResNo": "1"}})
    method, url = req.request.call_args.args[:2]
    assert method == "POST"
    assert url == "https://gateway.example.test/api/gateway/v1/trips"
    assert (
        req.request.call_args.kwargs["headers"]["Authorization"] == "Bearer lds_testkey1234567890"
    )
    assert req.request.call_args.kwargs["timeout"] == 10


def test_send_trip_success_returns_parsed_body():
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(
            json_data={"transactionId": "t-1", "reservationId": "R1", "totalAmount": "142.50"}
        )
        result = gnet.send_trip({})
    assert result == {"transactionId": "t-1", "reservationId": "R1", "totalAmount": "142.50"}


def test_send_trip_deduped_returns_body_without_pricing():
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(json_data={"transactionId": "t-1", "deduped": True})
        result = gnet.send_trip({})
    assert result == {"transactionId": "t-1", "deduped": True}
    assert "reservationId" not in result
    assert "totalAmount" not in result


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 502, 503])
def test_send_trip_error_statuses_raise_gnet_api_error(status):
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(status=status, text='{"error": "nope"}')
        with pytest.raises(gnet.GnetAPIError) as exc:
            gnet.send_trip({})
    assert exc.value.status == status
    assert "nope" in exc.value.body


def test_cancel_trip_deletes_by_transaction_id():
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(
            json_data={"success": True, "transactionId": "t-1", "status": "CANCELED"}
        )
        result = gnet.cancel_trip("t-1")
    method, url = req.request.call_args.args[:2]
    assert method == "DELETE"
    assert url == "https://gateway.example.test/api/gateway/v1/trips/t-1"
    assert (
        req.request.call_args.kwargs["headers"]["Authorization"] == "Bearer lds_testkey1234567890"
    )
    assert result["status"] == "CANCELED"


def test_cancel_trip_error_raises_gnet_api_error():
    with patch.object(gnet, "requests") as req:
        req.request.return_value = _response(status=404, text='{"error": "not found"}')
        with pytest.raises(gnet.GnetAPIError) as exc:
            gnet.cancel_trip("nope")
    assert exc.value.status == 404
