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

from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type

import requests
from django.conf import settings
from requests.exceptions import RequestException

from apps.dispatch.models import Assignment
from apps.leads.models import VehicleType
from apps.reservations.models import Stop

API_PATH = "/api/gateway/v1/trips"
TIMEOUT = 10  # seconds — an unbounded call can hang a worker indefinitely

# `requesterResNo` is `f"{RESNO_PREFIX}{assignment.pk}"`. The pk alone is correct but
# has almost no entropy, and the gateway's fallback correlation (a callback carrying no
# transactionId) matches resNo across ALL of its clients and refuses on a tie — so a
# bare "7" would be one collision away from an unroutable callback. The prefix keeps the
# pk semantics and namespaces them to us. `apps.dispatch.gnet_callback` strips it back
# off when correlating; change the two together.
RESNO_PREFIX = "apc-"

# GnetAPIError.status for a request that never got an HTTP response at all — a
# requests.exceptions.RequestException (connection refused, DNS failure, timeout,
# too many redirects, ...). See _request's docstring for why this is treated as
# unretryable, same as any other GnetAPIError.
TRANSPORT_FAILED = 0

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
    """A non-2xx response from the GNet gateway, OR a transport failure that never got
    a response at all (`.status == TRANSPORT_FAILED`, i.e. `0` — see `_request`).
    Carries `.status` and `.body` so a caller (Task 3) can branch on the status — see
    contract v2 §5.3/§5.7: 409 must never be retried, 502/503 are worth retrying,
    400/401/403/422 are not. `TRANSPORT_FAILED` is treated as unretryable too — see
    `_request`'s docstring for why."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"GNet gateway {status}: {body[:600]}")


class GnetNotConfigured(Exception):
    """Raised when a trip can't be represented in GNet's wire format locally — an
    unmapped vehicle type, a vendor with no `gnet_grid_id`, or a reservation with
    fewer than 2 stops. Refusing the send here, before any network call, is
    deliberate: it turns a silent partner-side 400 (or a raw `IndexError`) into a
    clear local error naming exactly what to fix."""


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


def _combine_iso(date_val: date_type | None, time_val: time_type | None) -> str | None:
    """Naive `date`+`time` -> ISO 8601 string, or None if either half is missing.

    No timezone is ever attached — the contract's timestamps are naive and irregular
    by design (§1.4/§5.11), so this never invents a conversion, only a format.
    """
    if not date_val or not time_val:
        return None
    return datetime.combine(date_val, time_val).isoformat()


def _location(stop: Stop, *, time_iso: str | None = None) -> dict:
    """One `locations.*` entry (pickup/dropOff/a `stops[]` element) for `stop`.

    `locationType` is hardcoded to `"address"` everywhere — deliberately, not an
    oversight: GNet's real `"airport"` type additionally wants `flightInfo`, and
    `Stop` captures no flight data at all. An address string plus precise
    coordinates (below) is enough for a driver to route on regardless of pickup
    type. Keys with no value (`lat`/`lon`, `time`) are omitted rather than sent as
    null — the payload is passthrough, so an absent key reaches GNet as "we don't
    have this," not as a value to act on.
    """
    location = {"locationType": "address", "address": stop.address}
    if stop.latitude is not None and stop.longitude is not None:
        # Floats, not strings — this is the one place a number is correct in this
        # payload (see GNET farm-out doc's worked example); money stays a string.
        location["lat"] = float(stop.latitude)
        location["lon"] = float(stop.longitude)
    if time_iso:
        location["time"] = time_iso
    return location


def _split_passenger_name(name: str) -> tuple[str, str]:
    """Same placeholder policy as la_sync._split_name (kept separate: two integrations,
    two contracts). GNet rejects a send with no passengers ("passenger list missing"),
    so a blank contact name still has to produce a valid entry."""
    first, _, last = (name or "").strip().partition(" ")
    return first or "Customer", (last.strip() or "-")


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

    Times are sent verbatim with no timezone conversion invented — the contract's
    timestamps are naive and irregular by design (§1.4/§5.11). Pickup/dropOff use
    the reservation's own `pickup_date`/`pickup_time` and `dropoff_date`/
    `dropoff_time`; an intermediate stop has no date of its own (`Stop.scheduled_time`
    is rendered against the trip's `pickup_date` — see the model), so its `time` is
    that same `pickup_date` combined with its own `scheduled_time`.

    Every stop on the trip reaches GNet: the first and last of `ordered_stops` become
    `locations.pickup`/`.dropOff`, and everything between is `locations.stops` — a
    first-class array in GNet's farm-out contract, always sent (even empty) rather
    than omitted, matching the doc's own `"stops": []` example. No stop is ever
    silently dropped between the two.

    Raises GnetNotConfigured — refusing the send locally rather than earning an
    opaque gateway 400 (or, for the stop count, a raw `IndexError`) — for an
    unmapped vehicle type, fewer than 2 stops (a reservation always needs at least
    a pickup and a drop-off; the reservation editor enforces this, but there is no
    model-level constraint, so a stray admin edit or import could still produce
    one), or a vendor with no `gnet_grid_id` set.
    """
    reservation = assignment.reservation
    vendor = assignment.vendor
    vehicle_type_code = vehicle_code(reservation.vehicle)
    passenger_first, passenger_last = _split_passenger_name(reservation.lead.contact.name)

    if not vendor.gnet_grid_id:
        raise GnetNotConfigured(
            f"Vendor {vendor.name!r} has no GNet griddID set — it can't be a farm-out "
            "provider until one is."
        )

    stops = list(reservation.ordered_stops)
    if len(stops) < 2:
        raise GnetNotConfigured(
            f"Reservation #{reservation.pk} has {len(stops)} stop(s) — a trip needs at "
            "least a pickup and a drop-off before it can be farmed out over GNet."
        )
    pickup_stop, dropoff_stop, middle_stops = stops[0], stops[-1], stops[1:-1]

    pickup = _location(
        pickup_stop, time_iso=_combine_iso(reservation.pickup_date, reservation.pickup_time)
    )
    dropoff = _location(
        dropoff_stop, time_iso=_combine_iso(reservation.dropoff_date, reservation.dropoff_time)
    )
    stops_payload = [
        _location(stop, time_iso=_combine_iso(reservation.pickup_date, stop.scheduled_time))
        for stop in middle_stops
    ]

    return {
        "affiliateReservation": {
            "requesterResNo": f"{RESNO_PREFIX}{assignment.pk}",
            # Stripped, because `Vendor.is_gnet_capable` strips before deciding this
            # vendor is on the network: a griddID pasted with a trailing space routes
            # here and would otherwise go out as `"gnet-42 "` and match no partner.
            "providerId": vendor.gnet_grid_id.strip(),
        },
        "preferredVehicleType": vehicle_type_code,
        "reservationType": reservation.trip_type.upper(),
        "passengerCount": str(reservation.passengers),
        # GNet's rules engine requires the list, not just the count — a send without
        # it is rejected with "passenger list missing" (learned live, 2026-08-28).
        "passengers": [{"firstName": passenger_first, "lastName": passenger_last}],
        "locations": {
            "pickup": pickup,
            "dropOff": dropoff,
            "stops": stops_payload,
        },
    }


def _request(method: str, path: str, *, json: dict | None = None) -> dict:
    """Make one HTTP call to the gateway, raising `GnetAPIError` for every failure —
    including one that never reached the gateway at all.

    A `requests.exceptions.RequestException` (connection refused, DNS failure, a
    `Timeout`, too many redirects, ...) is caught here and re-raised as
    `GnetAPIError(TRANSPORT_FAILED, ...)` rather than escaping raw. This matters
    because a raw exception would bypass `push_assignment`/`cancel_assignment`'s
    `except GnetAPIError` entirely (see `apps.dispatch.gnet_sync`): the `GnetEvent`
    would stay PENDING forever instead of being marked ERROR, no one would be
    alerted, and — because PENDING isn't one of the two terminal results that
    short-circuit a re-push — a second call would resend under the exact same
    `requesterResNo`. For a plain connection failure that's harmless (nothing ever
    reached the gateway), but for a `Timeout` it is not: the request may well have
    arrived and booked a real vehicle before the response was lost, and there is no
    reliable way from here to tell the two cases apart. So BOTH are treated as
    unretryable, exactly like a 409 — recovery is the same as any other gateway
    failure: reconcile manually in GNet, then farm the trip out again as a new
    `Assignment`, which gets a fresh pk and therefore a fresh, safe `requesterResNo`.
    """
    try:
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
    except RequestException as exc:
        raise GnetAPIError(TRANSPORT_FAILED, f"transport failure: {exc}") from exc
    if resp.status_code >= 400:
        raise GnetAPIError(resp.status_code, resp.text or "")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        # A 2xx with an unparseable body (a proxy hiccup, an HTML error page slipping
        # through with a 200) is still a failure — surface it the same way callers
        # already handle every other failure, not as a raw exception from `resp.json`.
        raise GnetAPIError(resp.status_code, resp.text or "") from exc


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
