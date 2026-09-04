"""Parse + persist the Alpine reservation 'draft' payload (leads/quotes spec §4)."""

from __future__ import annotations

import re
from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .flights import link_flights
from .models import Reservation, Stop


class DraftError(ValueError):
    """Invalid reservation draft payload — surfaced to the client as HTTP 400."""


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, TypeError) as exc:
        raise DraftError(f"invalid number: {value!r}") from exc
    if amount < 0:
        raise DraftError("amounts cannot be negative")
    return amount


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DraftError(f"invalid date: {value!r}") from exc


def _time(value) -> time | None:
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DraftError(f"invalid time: {value!r}") from exc


def _vehicle_id(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _coord(value, limit: int) -> Decimal | None:
    """A stop coordinate from the editor. Out-of-range or unparseable → None, so a
    malformed client payload degrades to 'geocode it later' rather than 400-ing a save."""
    if value in (None, ""):
        return None
    try:
        coord = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not -limit <= coord <= limit:
        return None
    return coord.quantize(Decimal("0.000001"))


FLIGHT_RE = re.compile(r"^\d{1,6}$")  # public: imported cross-module by apps/reservations/views.py
# A US-registered tail number: "N" + up to 5 alphanumerics (e.g. "N561FX") — the only shape
# `Stop.flight_number` holds for the seeded Private carrier (2026-08-29 §2). `Stop.flight_number`
# is already `max_length=6`, exactly wide enough, so no column change was needed.
TAIL_RE = re.compile(r"^N[0-9A-Z]{1,5}$")  # public: imported cross-module by views.py


def _pk(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _raw_flight_number(value) -> str:
    """Just the trimmed text — format isn't validated until `_validate_flight_info` knows
    which airline was chosen (a tail number is only valid for the Private carrier)."""
    return str(value or "").strip()


def _validated_flight_number(value: str, *, is_private: bool) -> str:
    """Digits only for a real carrier; a tail number (case-insensitive on input, stored
    upper-case) for the seeded Private one."""
    if not value:
        return value
    if is_private:
        text = value.upper()
        if not TAIL_RE.match(text):
            raise DraftError("tail number must start with N (up to 6 characters)")
        return text
    if not FLIGHT_RE.match(value):
        raise DraftError("flight number must be digits (up to 6)")
    return value


_DIRECTIONS = ("", "arrival", "departure")


def _direction(value) -> str:
    text = str(value or "").strip().lower()
    if text not in _DIRECTIONS:
        raise DraftError("choose arriving or departing")
    return text


def _apply_flight_directions(stops: list[dict]) -> None:
    """The ends are fixed by position — a pickup meets an arrival, a drop-off catches a
    departure — whatever the client sent; a middle stop keeps its choice (or blank). No
    airport → no direction (spec §4.2)."""
    last = len(stops) - 1
    for i, stop in enumerate(stops):
        if stop["airport_id"] is None:
            stop["flight_direction"] = ""
        elif i == 0:
            stop["flight_direction"] = "arrival"
        elif i == last:
            stop["flight_direction"] = "departure"


def _validate_flight_info(
    stops: list[dict], grandfathered_airline_ids: frozenset[int] = frozenset()
) -> None:
    """Airline / flight only mean anything at an airport, and both the airport and the
    airline must be real rows — the client only ever sends what search / the picker
    handed it, so anything else is a stale or forged payload. Two queries, not two per
    stop.

    A carrier retired (`is_active=False`) after a stop was booked stays on that stop
    across unrelated edits (spec §3.1): its pk is passed in via
    `grandfathered_airline_ids` and skips the active-airline check. New choices still
    come from the active list only.

    Each stop's `flight_number` arrives here as trimmed-but-unvalidated text
    (`_raw_flight_number`) — its format depends on which airline was chosen (a tail
    number is only valid for the seeded Private carrier), which this function is the
    first place to actually know, so the digits-vs-tail-number check happens here too,
    on the same airline query, rather than costing a third.
    """
    from apps.addresses.models import PRIVATE_AIRLINE_IATA, Airline, Airport

    for stop in stops:
        if stop["airport_id"] is None:
            stop["airline_id"], stop["flight_number"] = None, ""
    airport_ids = {s["airport_id"] for s in stops if s["airport_id"] is not None}
    if airport_ids:
        known = set(Airport.objects.filter(pk__in=airport_ids).values_list("pk", flat=True))
        if airport_ids - known:
            raise DraftError("unknown airport")
    airline_ids = {s["airline_id"] for s in stops if s["airline_id"] is not None}
    unknown = airline_ids - grandfathered_airline_ids
    private_ids: set[int] = set()
    if airline_ids:
        rows = Airline.objects.filter(pk__in=airline_ids).values_list("pk", "iata", "is_active")
        active_pks = set()
        for pk, iata, is_active in rows:
            if is_active:
                active_pks.add(pk)
            if iata == PRIVATE_AIRLINE_IATA:
                private_ids.add(pk)
        if unknown - active_pks:
            raise DraftError("choose an airline from the list")
    for stop in stops:
        stop["flight_number"] = _validated_flight_number(
            stop["flight_number"], is_private=stop["airline_id"] in private_ids
        )


def parse_draft(payload: dict, *, grandfathered_airline_ids: frozenset[int] = frozenset()) -> dict:
    """Validate + normalise a draft into model kwargs (+ a `stops` list)."""
    trip_type = payload.get("tripType") or payload.get("trip_type")
    if trip_type not in (Reservation.TripType.TRANSFER, Reservation.TripType.HOURLY):
        raise DraftError(f"invalid trip type: {trip_type!r}")

    raw_stops = [s for s in (payload.get("stops") or []) if isinstance(s, dict)]
    if len(raw_stops) < 2:
        raise DraftError("a reservation needs at least a pickup and a drop-off")

    try:
        pax = max(1, int(payload.get("pax") or 1))
    except (ValueError, TypeError) as exc:
        raise DraftError("invalid passenger count") from exc

    data = {
        "trip_type": trip_type,
        "service_type_id": _pk(payload.get("serviceType")),
        "pickup_date": _date(payload.get("date")),
        "pickup_time": _time(payload.get("time")),
        "vehicle_id": _vehicle_id(payload.get("vehicle")),
        "passengers": pax,
        "rate": _money(payload.get("rate")),
        "hours": _money(payload.get("hours")),
        "min_hours": _money(payload.get("minHours")),
        "gratuity_pct": _money(payload.get("gratuityPct")),
        "gratuity_flat": _money(payload.get("gratuityFlat")),
        "discount_pct": _money(payload.get("discountPct")),
        "discount_flat": _money(payload.get("discountFlat")),
        "dropoff_date": _date(payload.get("dropoffDate")),
        "dropoff_time": _time(payload.get("dropoffTime")),
        "stops": [
            {
                "address": (s.get("address") or "").strip()[:255],
                "note": (s.get("note") or "").strip()[:255],
                "name": (s.get("name") or "").strip()[:160],
                "scheduled_time": _time(s.get("time")),
                "latitude": _coord(s.get("lat"), 90),
                "longitude": _coord(s.get("lng"), 180),
                "airport_id": _pk(s.get("airport")),
                "airline_id": _pk(s.get("airline")),
                # No airport → the flight is dropped anyway, so a stale/garbled value
                # must not 400 the save (spec: "no airport → airline and flight are dropped").
                # Format (digits vs. tail number) isn't validated until
                # `_validate_flight_info` knows which airline was chosen.
                "flight_number": (
                    _raw_flight_number(s.get("flight")) if _pk(s.get("airport")) is not None else ""
                ),
                # Same rule as the flight number: without an airport it is dropped, so a
                # garbled value never 400s the save.
                "flight_direction": (
                    _direction(s.get("direction")) if _pk(s.get("airport")) is not None else ""
                ),
            }
            for s in raw_stops
        ],
    }
    _validate_flight_info(data["stops"], grandfathered_airline_ids)
    _apply_flight_directions(data["stops"])
    _derive_dropoff_and_hours(data, trip_type)
    _derive_endpoint_stop_times(data)
    return data


def _derive_endpoint_stop_times(data: dict) -> None:
    """Give the first/last stop the trip's pickup / drop-off time when they carry none.

    The editor asks for the trip's times once, so the two endpoint stops would otherwise
    reach the customer's itinerary (`public/quote.html`) with no time against them. Run
    after `_derive_dropoff_and_hours` so an hourly trip mirrors its *derived* drop-off.
    An explicit stop time always wins.
    """
    stops = data["stops"]
    if stops[0]["scheduled_time"] is None:
        stops[0]["scheduled_time"] = data.get("pickup_time")
    if stops[-1]["scheduled_time"] is None:
        stops[-1]["scheduled_time"] = data.get("dropoff_time")


def _derive_dropoff_and_hours(data: dict, trip_type: str) -> None:
    """Hourly: drop-off = pickup + billed hours. Transfer: only sanity-check the times.

    `hours` is the agent's override and is stored exactly as posted for both types; a
    transfer no longer derives it from drop-off − pickup (it is priced at the rate-card
    minimum unless overridden — spec 2026-08-28).
    """
    from datetime import datetime, timedelta

    pd, pt = data.get("pickup_date"), data.get("pickup_time")
    if trip_type == Reservation.TripType.HOURLY:
        billed = data["hours"] if data["hours"] > 0 else data["min_hours"]
        if pd and pt and billed:
            end = datetime.combine(pd, pt) + timedelta(hours=float(billed))
            data["dropoff_date"], data["dropoff_time"] = end.date(), end.time()
    else:
        dd, dt_ = data.get("dropoff_date"), data.get("dropoff_time")
        if pd and pt and dd and dt_ and datetime.combine(dd, dt_) <= datetime.combine(pd, pt):
            raise DraftError("drop-off must be after pickup")


@transaction.atomic
def save_reservation_from_draft(
    lead, payload: dict, instance: Reservation | None = None
) -> Reservation:
    """Create or update one reservation (+ its ordered stops) from a draft."""
    kept = (
        frozenset(instance.stops.exclude(airline_id=None).values_list("airline_id", flat=True))
        if instance is not None
        else frozenset()
    )
    data = parse_draft(payload, grandfathered_airline_ids=kept)
    stops = data.pop("stops")
    link_flights(stops, data.get("pickup_date"))
    is_new = instance is None
    prev_pickup = None if is_new else (instance.pickup_date, instance.pickup_time)
    if instance is None:
        instance = Reservation(lead=lead)
        last = lead.reservations.order_by("-sort_order").first()
        instance.sort_order = (last.sort_order + 1) if last else 0
    for field, value in data.items():
        setattr(instance, field, value)
    instance.save()
    instance.stops.all().delete()
    Stop.objects.bulk_create(
        [
            Stop(
                reservation=instance,
                sequence=i,
                address=s["address"],
                note=s["note"],
                name=s["name"],
                scheduled_time=s["scheduled_time"],
                latitude=s["latitude"],
                longitude=s["longitude"],
                airport_id=s["airport_id"],
                airline_id=s["airline_id"],
                flight_number=s["flight_number"],
                flight_direction=s["flight_direction"],
                flight_id=s["flight_id"],
            )
            for i, s in enumerate(stops)
        ]
    )
    instance.refresh_pickup_timezone()

    # Keep the per-trip service-date messaging (APC-18-22) in step with a booked order:
    # a new trip gets its rows, a moved pickup reschedules them.
    if lead.status == lead.Status.BOOKED:
        from apps.messaging import touchpoints

        if is_new:
            touchpoints.schedule_service_touchpoints(lead)
        elif prev_pickup != (instance.pickup_date, instance.pickup_time):
            touchpoints.reschedule_service_touchpoints(instance)
    return instance
