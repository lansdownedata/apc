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
import requests

from apps.dispatch.factories import AssignmentFactory
from apps.integrations import gnet
from apps.leads.factories import VehicleTypeFactory
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Stop
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


def _assignment_with_stops(*stop_kwargs_list, **reservation_kwargs):
    """An assignment on a reservation whose stops are exactly `stop_kwargs_list`, in
    order — each a dict of Stop field overrides (address required)."""
    vendor = VendorFactory(gnet_grid_id="gnet-partner-42")
    vehicle = VehicleTypeFactory(name="Luxury Sedan")
    reservation = ReservationFactory(vehicle=vehicle, stops=[], **reservation_kwargs)
    for sequence, stop_kwargs in enumerate(stop_kwargs_list):
        Stop.objects.create(reservation=reservation, sequence=sequence, **stop_kwargs)
    return AssignmentFactory(reservation=reservation, vendor=vendor, payout=Decimal("300.00"))


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
    """The resNo is namespaced: the gateway's fallback correlation matches resNo across
    ALL its clients and refuses on a tie, so a bare pk like "7" is a collision waiting to
    happen. The prefix keeps the pk semantics and removes the ambiguity."""
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["affiliateReservation"]["requesterResNo"] == f"apc-{assignment.pk}"
    assert payload["affiliateReservation"]["providerId"] == assignment.vendor.gnet_grid_id


def test_payload_strips_the_provider_id():
    """`Vendor.is_gnet_capable` strips before deciding the vendor is on the network, so
    a griddID pasted with a trailing space passes routing — it must not then go out as
    `"gnet-42 "` and fail to match a partner."""
    assignment = _assignment()
    assignment.vendor.gnet_grid_id = "  gnet-partner-42  "
    payload = gnet.build_send_payload(assignment)
    assert payload["affiliateReservation"]["providerId"] == "gnet-partner-42"


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


def test_payload_raises_for_vendor_without_grid_id():
    """Task 4's routing means a non-GNet vendor should never reach this path, but the
    guard is defence in depth: refuse locally rather than send an empty providerId
    and let the gateway reject it remotely."""
    vendor = VendorFactory(gnet_grid_id="")
    vehicle = VehicleTypeFactory(name="Luxury Sedan")
    reservation = ReservationFactory(vehicle=vehicle)
    assignment = AssignmentFactory(reservation=reservation, vendor=vendor)
    with pytest.raises(gnet.GnetNotConfigured):
        gnet.build_send_payload(assignment)


def test_payload_raises_for_reservation_with_no_stops():
    """Not reachable through the reservation editor (it enforces a pickup + drop-off),
    but there is no model-level constraint — an admin edit or import could still
    produce one, and stops[0] on an empty list would otherwise raise a raw
    IndexError on a call that books real work."""
    assignment = _assignment_with_stops()
    with pytest.raises(gnet.GnetNotConfigured):
        gnet.build_send_payload(assignment)


def test_payload_raises_for_reservation_with_one_stop():
    """A single stop would otherwise send pickup and dropOff as the SAME stop."""
    assignment = _assignment_with_stops({"address": "Only Stop"})
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


def test_payload_two_stop_trip_sends_empty_stops_array():
    """GNet's own worked example sends `"stops": []` explicitly on a trip with no
    intermediate stops — the key must be present, not merely empty-or-absent."""
    assignment = _assignment()
    payload = gnet.build_send_payload(assignment)
    assert payload["locations"]["stops"] == []


def test_payload_intermediate_stops_appear_in_sequence_order():
    assignment = _assignment_with_stops(
        {"address": "Pickup Address"},
        {"address": "Stop One"},
        {"address": "Stop Two"},
        {"address": "Dropoff Address"},
    )
    payload = gnet.build_send_payload(assignment)
    assert payload["locations"]["pickup"]["address"] == "Pickup Address"
    assert payload["locations"]["dropOff"]["address"] == "Dropoff Address"
    stops_payload = payload["locations"]["stops"]
    assert [s["address"] for s in stops_payload] == ["Stop One", "Stop Two"]


def test_payload_never_drops_a_stop():
    """The property that actually matters: a lost stop means a driver never learns
    about it. Regardless of how many stops split across pickup/dropOff/stops, every
    address on the reservation must appear somewhere in the payload."""
    addresses = ["A Address", "B Address", "C Address", "D Address", "E Address"]
    assignment = _assignment_with_stops(*({"address": a} for a in addresses))
    payload = gnet.build_send_payload(assignment)
    locations = payload["locations"]
    seen = {locations["pickup"]["address"], locations["dropOff"]["address"]}
    seen |= {s["address"] for s in locations["stops"]}
    assert seen == set(addresses)


def test_payload_location_with_coordinates_emits_float_lat_lon():
    assignment = _assignment_with_stops(
        {
            "address": "Pickup",
            "latitude": Decimal("34.066470"),
            "longitude": Decimal("-118.399324"),
        },
        {"address": "Dropoff"},
    )
    payload = gnet.build_send_payload(assignment)
    pickup = payload["locations"]["pickup"]
    assert pickup["lat"] == pytest.approx(34.066470)
    assert pickup["lon"] == pytest.approx(-118.399324)
    assert isinstance(pickup["lat"], float)
    assert isinstance(pickup["lon"], float)


def test_payload_location_without_coordinates_omits_lat_lon():
    assignment = _assignment_with_stops({"address": "Pickup"}, {"address": "Dropoff"})
    payload = gnet.build_send_payload(assignment)
    dropoff = payload["locations"]["dropOff"]
    assert "lat" not in dropoff
    assert "lon" not in dropoff


def test_payload_intermediate_stop_time_uses_reservation_pickup_date():
    assignment = _assignment_with_stops(
        {"address": "Pickup"},
        {"address": "Middle", "scheduled_time": time(14, 30)},
        {"address": "Dropoff"},
        pickup_date=date(2026, 9, 1),
        pickup_time=time(14, 0),
    )
    payload = gnet.build_send_payload(assignment)
    middle = payload["locations"]["stops"][0]
    assert middle["time"] == "2026-09-01T14:30:00"


def test_payload_intermediate_stop_omits_time_when_not_scheduled():
    assignment = _assignment_with_stops(
        {"address": "Pickup"}, {"address": "Middle"}, {"address": "Dropoff"}
    )
    payload = gnet.build_send_payload(assignment)
    middle = payload["locations"]["stops"][0]
    assert "time" not in middle


def test_payload_carries_no_money_at_all():
    """On GNet the AFFILIATE prices the trip and quotes it back via the response's
    totalAmount (superseded later by the CLOSE callback) — sendTripSchema has no
    money field, so APC never dictates a payout here the way it does in the manual
    trip-sheet email. Neither the confidential customer price nor our own payout
    belongs in the send payload; assert both are absent."""
    assignment = _assignment()
    reservation = assignment.reservation
    assert reservation.line_total == Decimal("1500.00")  # rate 500 x 3h, no gratuity
    assert assignment.payout == Decimal("300.00")
    payload = gnet.build_send_payload(assignment)
    serialized = json.dumps(payload)
    assert "1500.00" not in serialized
    assert "300.00" not in serialized
    assert str(reservation.line_total) not in serialized
    assert "payoutAmount" not in serialized


def test_payload_never_leaks_customer_price():
    """Brokerage margin is confidential — the customer price must never reach the
    affiliate-facing payload, independent of whether any other amount is sent."""
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


def test_send_trip_malformed_json_on_2xx_raises_gnet_api_error():
    """A 2xx with an unparseable body (a proxy hiccup) must surface as the same
    GnetAPIError callers already handle everywhere else, not a raw exception."""
    with patch.object(gnet, "requests") as req:
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"<html>not json</html>"
        resp.text = "<html>not json</html>"
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        req.request.return_value = resp
        with pytest.raises(gnet.GnetAPIError) as exc:
            gnet.send_trip({})
    assert exc.value.status == 200
    assert "not json" in exc.value.body


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


# --- transport failures: a RequestException must become a GnetAPIError, not escape raw ---


@pytest.mark.parametrize(
    "transport_error",
    [
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.TooManyRedirects("too many redirects"),
    ],
)
def test_send_trip_transport_failure_becomes_gnet_api_error(transport_error):
    """A requests.exceptions.RequestException must never escape _request raw — every
    caller (apps.dispatch.gnet_sync) only catches GnetAPIError, so a raw transport
    exception would skip the terminal ERROR/alert path entirely and leave the
    GnetEvent stuck PENDING, free to be resent under the same requesterResNo."""
    with patch.object(gnet, "requests") as req:
        req.request.side_effect = transport_error
        with pytest.raises(gnet.GnetAPIError) as exc:
            gnet.send_trip({})
    assert exc.value.status == gnet.TRANSPORT_FAILED
    assert str(transport_error) in exc.value.body


def test_cancel_trip_transport_failure_becomes_gnet_api_error():
    with patch.object(gnet, "requests") as req:
        req.request.side_effect = requests.exceptions.ConnectionError("refused")
        with pytest.raises(gnet.GnetAPIError) as exc:
            gnet.cancel_trip("t-1")
    assert exc.value.status == gnet.TRANSPORT_FAILED


def test_transport_failure_status_is_distinct_from_any_real_http_status():
    """TRANSPORT_FAILED (0) must not collide with a real HTTP status code, since
    callers branch on `.status` to decide retry policy."""
    assert gnet.TRANSPORT_FAILED == 0
