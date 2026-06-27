"""Parse + persist the Alpine reservation 'draft' payload (leads/quotes spec §4)."""

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


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DraftError(f"invalid date: {value!r}") from exc


def _time(value):
    if not value:
        return None
    try:
        return time.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DraftError(f"invalid time: {value!r}") from exc


def _vehicle_id(value):
    try:
        return int(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


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

    return {
        "trip_type": trip_type,
        "service": (payload.get("service") or "").strip()[:120],
        "pickup_date": _date(payload.get("date")),
        "pickup_time": _time(payload.get("time")),
        "vehicle_id": _vehicle_id(payload.get("vehicle")),
        "passengers": pax,
        "base_rate": _money(payload.get("baseRate")),
        "hours": _money(payload.get("hours")),
        "hourly_rate": _money(payload.get("hourlyRate")),
        "min_hours": _money(payload.get("minHours")),
        "stops": [
            {
                "address": (s.get("address") or "").strip()[:255],
                "note": (s.get("note") or "").strip()[:255],
            }
            for s in raw_stops
        ],
    }


@transaction.atomic
def save_reservation_from_draft(lead, payload: dict, instance=None) -> Reservation:
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
            Stop(reservation=instance, sequence=i, address=s["address"], note=s["note"])
            for i, s in enumerate(stops)
        ]
    )
    return instance
