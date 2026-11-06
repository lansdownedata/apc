"""Farm-in receive service: turns a GNet reservation payload into portal facts.

Contract: GNET-CONNECTION-GUIDE.md §1. This module is pure interpretation and pricing;
persistence and responses live in later layers.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from apps.contacts.models import Company, Contact
from apps.core.choices import Channel
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


def _dict(value) -> dict:
    """``value`` when it is a dict, else an empty dict (payloads are untrusted)."""
    return value if isinstance(value, dict) else {}


def _passenger_count(raw, fallback: int) -> int:
    """Headcount from a GNet string; cosmetic, so junk falls back and the floor is 1."""
    try:
        count = int(str(raw).strip())
    except ValueError:
        count = fallback
    return max(count, 1)


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
    locations = _dict(payload.get("locations"))
    pickup = _dict(locations.get("pickup"))
    dropoff = _dict(locations.get("dropOff"))

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

    passengers = payload.get("passengers")
    passengers = passengers if isinstance(passengers, list) else []
    lead = _dict(passengers[0]) if passengers else {}
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
        passenger_count=_passenger_count(payload.get("passengerCount"), len(passengers) or 1),
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


def ensure_requester(payload: dict) -> tuple[Company, bool]:
    """Find or create the requesting operator's Company; call inside transaction.atomic()."""
    raw = _dict(payload.get("affiliateReservation")).get("requesterId")
    if isinstance(raw, bool) or not isinstance(raw, str | int):
        raise ValueError("requesterId missing")
    rid = str(raw).strip()
    if not rid:
        raise ValueError("requesterId missing")
    if len(rid) > 64:
        raise ValueError("requesterId too long")
    company = Company.objects.select_for_update().filter(gnet_grid_id=rid).first()
    if company:
        return company, False
    company = Company.objects.filter(name__iexact=rid, gnet_grid_id__isnull=True).first()
    if company:
        company.gnet_grid_id = rid
        company.is_affiliate_account = True
        company.save()
        return company, False
    company = Company.objects.create(
        name=rid,
        gnet_grid_id=rid,
        is_affiliate_account=True,
        notes="Auto-created from GNet farm-in",
    )
    Contact.objects.create(name=rid, company=company, channel=Channel.GNET)
    return company, True
