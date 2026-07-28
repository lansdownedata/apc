"""Parse + persist the Alpine reservation 'draft' payload (leads/quotes spec §4)."""

from __future__ import annotations

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


def parse_draft(payload: dict) -> dict:
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
            }
            for s in raw_stops
        ],
    }
    _derive_dropoff_and_hours(data, trip_type)
    return data


def _derive_dropoff_and_hours(data: dict, trip_type: str) -> None:
    """Hourly: drop-off = pickup + hours. Transfer: hours = drop-off − pickup."""
    from datetime import datetime, timedelta

    pd, pt = data.get("pickup_date"), data.get("pickup_time")
    if trip_type == Reservation.TripType.HOURLY:
        if pd and pt and data["hours"]:
            end = datetime.combine(pd, pt) + timedelta(hours=float(data["hours"]))
            data["dropoff_date"], data["dropoff_time"] = end.date(), end.time()
    else:  # transfer — hours come from the entered drop-off
        dd, dt_ = data.get("dropoff_date"), data.get("dropoff_time")
        if pd and pt and dd and dt_:
            start = datetime.combine(pd, pt)
            end = datetime.combine(dd, dt_)
            if end <= start:
                raise DraftError("drop-off must be after pickup")
            data["hours"] = Decimal(str(round((end - start).total_seconds() / 3600, 2)))


@transaction.atomic
def save_reservation_from_draft(
    lead, payload: dict, instance: Reservation | None = None
) -> Reservation:
    """Create or update one reservation (+ its ordered stops) from a draft."""
    data = parse_draft(payload)
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
            )
            for i, s in enumerate(stops)
        ]
    )
    return instance
