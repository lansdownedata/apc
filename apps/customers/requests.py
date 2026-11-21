"""What a signed-in customer changes or asks for from the portal.

Every write to an existing trip resolves it through `selectors.visible_trips(account)`, so
an id from another account is skipped, never written. `Reservation` is audited: each
changed trip is saved on its own with `update_fields`, so the trail records it under the
request's source (`customer portal`, `apps.audit.middleware`). A requested trip is a new
lead on the account's own contact (`request_trip`).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.audit.children import track_children
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.flights import link_flights
from apps.reservations.models import Passenger, Reservation, Stop

from .models import CustomerAccount
from .selectors import visible_trips
from .services import CustomerError

REFERENCE_MAX = Reservation._meta.get_field("customer_reference").max_length


def set_reference(account: CustomerAccount, trip_ids: Iterable[int], value: str) -> int:
    """Set "Your reference" on each of the account's trips in `trip_ids`, trimmed and cut
    to `REFERENCE_MAX` characters (blank clears it). Returns how many trips changed."""
    value = value.strip()[:REFERENCE_MAX].strip()
    trips = visible_trips(account).filter(pk__in=set(trip_ids)).prefetch_related(None)
    changed = 0
    with transaction.atomic():
        for trip in trips:
            if trip.customer_reference == value:
                continue
            trip.customer_reference = value
            trip.save(update_fields=["customer_reference", "updated_at"])
            changed += 1
    return changed


# --- request a trip --------------------------------------------------------------------

# Per account, as the planner request limit (`planners.services.REQUEST_LIMIT`).
REQUEST_LIMIT = 10
REQUEST_WINDOW = timedelta(hours=1)
# The same date, time and pickup from the same account inside this window is a double
# click on Send, not a second request.
DUPLICATE_WINDOW = timedelta(seconds=30)
# How many of the account's latest stops `recent_addresses` reads to find its few.
RECENT_SCAN = 200


def _requests(account: CustomerAccount) -> QuerySet[Lead]:
    return Lead.objects.filter(contact_id=account.contact_id, channel=Channel.PORTAL)


def _duplicate(account: CustomerAccount, data: dict, now: datetime) -> Lead | None:
    stops = data.get("stops") or []
    return (
        _requests(account)
        .filter(
            created_at__gte=now - DUPLICATE_WINDOW,
            reservations__pickup_date=data.get("pickup_date"),
            reservations__pickup_time=data.get("pickup_time"),
            reservations__stops__sequence=0,
            reservations__stops__address=stops[0]["address"] if stops else "",
        )
        .order_by("created_at", "pk")
        .first()
    )


def _notes(data: dict) -> str:
    """The customer's notes, with the vehicle they would like on a line of its own: staff
    still choose the vehicle."""
    vehicle = data.get("vehicle_preference")
    parts = [(data.get("notes") or "").strip()]
    if vehicle is not None:
        parts.append(f"Vehicle preference: {vehicle.name}")
    return "\n\n".join(part for part in parts if part)


def _rider(account: CustomerAccount, data: dict) -> tuple[str, str]:
    """(name, mobile) of the trip's lead passenger: who trip-day texts go to."""
    if data.get("rider") == "other":
        return data["rider_name"], data["rider_mobile"]
    contact = account.contact
    return contact.name, to_e164(contact.phone) or ""


def _with_directions(stops: list[dict]) -> list[dict]:
    """`public.services.create_lead_from_booking`'s rule: the ends are fixed (the pickup
    meets an arrival, the drop-off a departure), a middle stop keeps what was chosen, and
    no airport means no direction."""
    last = len(stops) - 1
    for i, stop in enumerate(stops):
        if not stop.get("airport_id"):
            stop["flight_direction"] = ""
        elif i == 0:
            stop["flight_direction"] = "arrival"
        elif i == last:
            stop["flight_direction"] = "departure"
        else:
            stop.setdefault("flight_direction", "")
    return stops


def request_trip(account: CustomerAccount, data: dict) -> Lead:
    """Turn a valid `TripRequestForm` into a New lead on the account's own contact, with
    one unpriced trip shaped as a website booking's (`create_lead_from_booking`). The
    contact is never matched, renamed or given a new number.

    The same request again inside `DUPLICATE_WINDOW` returns the first lead. Past
    `REQUEST_LIMIT` requests in the rolling hour, raises `CustomerError("limited")`. Both
    checks and the create run under a lock on the account, so a double click can't slip
    two leads past either."""
    contact = account.contact
    with transaction.atomic():
        CustomerAccount.objects.select_for_update().filter(pk=account.pk).first()
        now = timezone.now()
        duplicate = _duplicate(account, data, now)
        if duplicate is not None:
            return duplicate
        if _requests(account).filter(created_at__gte=now - REQUEST_WINDOW).count() >= (
            REQUEST_LIMIT
        ):
            raise CustomerError("limited")
        lead = Lead.objects.create(
            contact=contact, status=Lead.Status.NEW, channel=Channel.PORTAL, notes=_notes(data)
        )
        reservation = Reservation.objects.create(
            lead=lead,
            trip_type=data.get("trip_type") or Reservation.TripType.TRANSFER,
            hours=data.get("hours") or 0,
            service_type=data.get("service_type"),
            pickup_date=data.get("pickup_date"),
            pickup_time=data.get("pickup_time"),
            passengers=data.get("passengers") or 1,
            customer_reference=data.get("customer_reference") or "",
        )
        stops = _with_directions(data.get("stops") or [])
        link_flights(stops, reservation.pickup_date)
        name, mobile = _rider(account, data)
        with track_children(reservation, route=True, passengers=True):
            Stop.objects.bulk_create(
                [
                    Stop(
                        reservation=reservation,
                        sequence=i,
                        address=s.get("address", ""),
                        latitude=s.get("lat"),
                        longitude=s.get("lng"),
                        airport_id=s.get("airport_id"),
                        airline_id=s.get("airline_id"),
                        flight_number=s.get("flight_number", ""),
                        flight_direction=s.get("flight_direction", ""),
                        flight_id=s.get("flight_id"),
                    )
                    for i, s in enumerate(stops)
                ]
            )
            Passenger.objects.create(
                reservation=reservation, name=name, mobile=mobile, is_lead=bool(mobile)
            )
        reservation.refresh_pickup_timezone()
        service_type = data.get("service_type")
        Notification.notify(
            lead,
            Notification.Kind.NEW_LEAD,
            title=f"New portal request: {contact.name}",
            detail=service_type.name if service_type else "",
        )
    return lead


def recent_addresses(account: CustomerAccount, limit: int = 5) -> list[dict]:
    """Up to `limit` distinct addresses (compared trimmed and case-folded) from the stops
    of the account's own trips, latest trip first: what the "Recent" chips fill in."""
    stops = (
        Stop.objects.filter(reservation__in=visible_trips(account).values("pk"))
        .exclude(address="")
        .order_by(
            F("reservation__pickup_date").desc(nulls_last=True),
            F("reservation__pickup_time").desc(nulls_last=True),
            "-reservation_id",
            "sequence",
        )
        .values("address", "latitude", "longitude", "airport_id")[:RECENT_SCAN]
    )
    seen: set[str] = set()
    recent: list[dict] = []
    for stop in stops:
        address = stop["address"].strip()
        if not address or address.casefold() in seen:
            continue
        seen.add(address.casefold())
        recent.append(
            {
                "address": address,
                "lat": float(stop["latitude"]) if stop["latitude"] is not None else None,
                "lng": float(stop["longitude"]) if stop["longitude"] is not None else None,
                "airport_id": stop["airport_id"],
            }
        )
        if len(recent) == limit:
            break
    return recent
