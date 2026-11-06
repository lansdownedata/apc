"""Farm-in receive service: turns a GNet reservation payload into portal facts.

Contract: GNET-CONNECTION-GUIDE.md §1. This module is pure interpretation and pricing;
persistence and responses live in later layers.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from apps.integrations import gnet
from apps.leads.models import VehicleType
from apps.reservations import timezones

ALERT_UNMAPPED = "Unmapped vehicle type: {code}"
ALERT_NO_PICKUP = "No pickup time in request"
ALERT_TZ_ASSUMED = "Time zone assumed from company default"

_CENT = Decimal("0.01")


@dataclass(slots=True)
class Summary:
    """What the dispatcher needs to see about an inbound request."""

    vehicle: VehicleType | None
    gnet_vehicle_type: str
    hourly: bool
    pickup_at: datetime | None
    timezone: str
    timezone_assumed: bool
    pickup_summary: str
    dropoff_summary: str
    passenger_name: str
    passenger_count: int
    alerts: list[str] = field(default_factory=list)


def _parse_pickup(raw, tz_name: str) -> datetime | None:
    """UTC pickup instant. Aware values convert; naive values are read in ``tz_name``."""
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(UTC)


def summarize(payload: dict) -> Summary:
    """Interpret a farm-in payload; problems become alerts rather than errors."""
    alerts: list[str] = []
    locations = payload.get("locations") or {}
    pickup = locations.get("pickup") or {}
    dropoff = locations.get("dropOff") or {}

    code = str(payload.get("preferredVehicleType") or "")
    vehicle = gnet.vehicle_type_for_code(code)
    if vehicle is None:
        alerts.append(ALERT_UNMAPPED.format(code=code))

    tz_name = timezones.timezone_at(pickup.get("lat"), pickup.get("lon"))
    assumed = tz_name is None
    if assumed:
        tz_name = settings.TIME_ZONE

    pickup_at = _parse_pickup(pickup.get("time"), tz_name)
    if pickup_at is None:
        alerts.append(ALERT_NO_PICKUP)
    if assumed:
        alerts.append(ALERT_TZ_ASSUMED)

    passengers = payload.get("passengers") or []
    lead = passengers[0] if passengers else {}
    name = f"{lead.get('firstName') or ''} {lead.get('lastName') or ''}".strip()

    return Summary(
        vehicle=vehicle,
        gnet_vehicle_type=code,
        hourly="hour" in str(payload.get("runType", "")).lower(),
        pickup_at=pickup_at,
        timezone=tz_name,
        timezone_assumed=assumed,
        pickup_summary=pickup.get("address") or "",
        dropoff_summary=dropoff.get("address") or "",
        passenger_name=name,
        passenger_count=int(payload.get("passengerCount") or len(passengers) or 1),
        alerts=alerts,
    )


def price_request(vehicle: VehicleType | None, *, hourly: bool) -> Decimal | None:
    """Rate-card price: rate x the applicable minimum hours; None when unpriceable."""
    if vehicle is None or vehicle.rate <= 0:
        return None
    hours = vehicle.hourly_min_hours if hourly else vehicle.transfer_min_hours
    if hours <= 0:
        return None
    return (vehicle.rate * hours).quantize(_CENT)
