"""Parse + persist the Alpine reservation 'draft' payload (leads/quotes spec §4)."""

from __future__ import annotations

import re
from datetime import date, time
from decimal import Decimal, InvalidOperation

from django.db import transaction

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


_FLIGHT_RE = re.compile(r"^\d{1,6}$")


def _pk(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _flight_number(value) -> str:
    text = str(value or "").strip()
    if text and not _FLIGHT_RE.match(text):
        raise DraftError("flight number must be digits (up to 6)")
    return text


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
    """
    from apps.addresses.models import Airline, Airport

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
    if unknown:
        active = set(
            Airline.objects.filter(pk__in=unknown, is_active=True).values_list("pk", flat=True)
        )
        if unknown - active:
            raise DraftError("choose an airline from the list")


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
        "service": (payload.get("service") or "").strip()[:120],
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
                "flight_number": (
                    _flight_number(s.get("flight")) if _pk(s.get("airport")) is not None else ""
                ),
            }
            for s in raw_stops
        ],
    }
    _validate_flight_info(data["stops"], grandfathered_airline_ids)
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
            )
            for i, s in enumerate(stops)
        ]
    )
    return instance
