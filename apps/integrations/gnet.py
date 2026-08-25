"""GNet farm-out gateway client (Lansdowne relay, contract v2 §5).

Lansdowne operates a single gateway in front of the real GNet partner network. This
module is the HTTP boundary only: pure functions that make a request only when called,
so callers (Task 3's orchestration: preview-mode gating, the GnetEvent audit log,
alerting) stay in full control of *when* a real network call — and therefore a real
booking with a real affiliate operator — happens.

Mirrors apps/integrations/podium.py's shape: module-level constants, a private
_request, typed public functions, one exception class carrying enough to branch on.

SAFETY: `api.grdd.net` is real production infrastructure behind this gateway. A
successful POST books a REAL vehicle with a REAL affiliate. Never call send_trip /
cancel_trip from a test — always mock `requests` at the boundary.
"""

from datetime import datetime

import requests
from django.conf import settings

from apps.dispatch.models import Assignment
from apps.leads.models import VehicleType

API_PATH = "/api/gateway/v1/trips"
TIMEOUT = 10  # seconds — an unbounded call can hang a worker indefinitely

# This project's six seeded VehicleType.name values (apps/core/management/commands/
# seed_demo.py) -> GNet's standardized, allowlisted preferredVehicleType codes
# (GNET-CONNECTION-GUIDE.md §3, src/lib/gnet/vehicle-types.ts in the gateway repo).
# There is NO bare "VAN" code — vans are always qualified (VAN_MINI, VAN_12, ...) —
# which is why "Sprinter Van" maps to SPRINTER rather than any VAN_* code.
VEHICLE_TYPE_MAP: dict[str, str] = {
    "Luxury Sedan": "SEDAN_LUX",
    "Luxury SUV": "SUV_LUX",
    "Sprinter Van": "SPRINTER",
    "Mini Coach": "BUS_20_25",  # seeded at 24 passengers
    "Motor Coach": "COACH",
    "Stretch Limousine": "LIMO",
}


class GnetAPIError(Exception):
    """A non-2xx response from the GNet gateway. Carries `.status` and `.body` so a
    caller (Task 3) can branch on the status — see contract v2 §5.3/§5.7: 409 must
    never be retried, 502/503 are worth retrying, 400/401/403/422 are not."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"GNet gateway {status}: {body[:600]}")


class GnetNotConfigured(Exception):
    """Raised when a trip can't be represented in GNet's wire format locally —
    currently just an unmapped vehicle type. Refusing the send here, before any
    network call, is deliberate: it turns a silent partner-side 400 into a clear
    local error naming exactly what to fix."""


def vehicle_code(vehicle: VehicleType) -> str:
    """Map a VehicleType to GNet's allowlisted `preferredVehicleType` code.

    Raises GnetNotConfigured for a name with no entry in VEHICLE_TYPE_MAP — e.g. a
    vehicle type added to the fleet after this map was written — rather than sending
    an unrecognized code the gateway would reject with an opaque 400.
    """
    try:
        return VEHICLE_TYPE_MAP[vehicle.name]
    except KeyError:
        raise GnetNotConfigured(
            f"No GNet vehicle-type mapping for {vehicle.name!r} — add it to "
            "VEHICLE_TYPE_MAP in apps/integrations/gnet.py before farming this out."
        ) from None


def build_send_payload(assignment: Assignment) -> dict:
    """Build the body for `POST /api/gateway/v1/trips` from one dispatch.Assignment.

    Deliberately excludes two things:
    - `transactionId` / `affiliateReservation.requesterId`: the gateway injects both
      (contract v2 §5.2) and rejects the whole request with 400 if either is sent.
    - Any amount at all. `sendTripSchema` (the gateway's actual validator) has no
      money field, and the §5.11 worked example confirms why: on GNet, the AFFILIATE
      prices the trip and quotes it back in the response's `totalAmount` (superseded
      later by the `CLOSE` callback's final figure) — APC does not dictate a payout
      the way it does in the manual trip-sheet email
      (`dispatch.services.offer_email_context`). Sending either the confidential
      customer price (`Reservation.line_total`) or our own `Assignment.payout` here
      would just be passed through and ignored, so neither goes out. `totalAmount` is
      read off the *response*, not sent in this payload.

    Times are sent verbatim from the trip-local `pickup_date`/`pickup_time` /
    `dropoff_date`/`dropoff_time` fields with no timezone conversion invented — the
    contract's timestamps are naive and irregular by design (§1.4/§5.11).
    """
    reservation = assignment.reservation
    vendor = assignment.vendor
    vehicle_type_code = vehicle_code(reservation.vehicle)

    stops = list(reservation.ordered_stops)
    pickup_stop, dropoff_stop = stops[0], stops[-1]

    pickup = {"locationType": "address", "address": pickup_stop.address}
    if reservation.pickup_date and reservation.pickup_time:
        pickup["time"] = datetime.combine(
            reservation.pickup_date, reservation.pickup_time
        ).isoformat()

    dropoff = {"locationType": "address", "address": dropoff_stop.address}
    if reservation.dropoff_date and reservation.dropoff_time:
        dropoff["time"] = datetime.combine(
            reservation.dropoff_date, reservation.dropoff_time
        ).isoformat()

    return {
        "affiliateReservation": {
            "requesterResNo": str(assignment.pk),
            "providerId": vendor.gnet_grid_id,
        },
        "preferredVehicleType": vehicle_type_code,
        "reservationType": reservation.trip_type.upper(),
        "passengerCount": str(reservation.passengers),
        "locations": {
            "pickup": pickup,
            "dropOff": dropoff,
        },
    }


def _request(method: str, path: str, *, json: dict | None = None) -> dict:
    resp = requests.request(
        method,
        f"{settings.GNET_GATEWAY_URL}{path}",
        headers={
            "Authorization": f"Bearer {settings.GNET_API_KEY}",
            "Accept": "application/json",
        },
        json=json,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise GnetAPIError(resp.status_code, resp.text or "")
    return resp.json() if resp.content else {}


def send_trip(payload: dict) -> dict:
    """POST a new trip. Persist `transactionId` from the response immediately (§5.6)
    — there is no lookup-by-`requesterResNo` endpoint to recover it later.

    A 200 with `deduped: true` means this `requesterResNo` was already sent and the
    original succeeded; treat it as success, but note the body carries only
    `transactionId`/`deduped` — never `reservationId`/`totalAmount` (persist those
    from the ORIGINAL 200, not this one).

    Raises GnetAPIError for every other non-2xx status, including 409 (the original
    send was claimed but never got a transactionId — an ambiguous failure). The
    caller must NEVER retry a 409 under the same `requesterResNo`: resend under a
    brand-new one only after an operator resolves it in the gateway's reconciliation
    view (§5.3).
    """
    return _request("POST", API_PATH, json=payload)


def cancel_trip(transaction_id: str) -> dict:
    """DELETE /trips/{transactionId} — cancel by transaction id, no body.

    If GNet rejects the cancellation, the transaction is left exactly as it was on
    the gateway's side too (§5.7); callers must not locally mark a trip cancelled
    just because this call raised.
    """
    return _request("DELETE", f"{API_PATH}/{transaction_id}")
