"""The order a farm-in request is worked as: one lead with one trip (spec 2026-11-07 §3).

`build` makes the lead and reservation when a request arrives; `apply_update` rewrites what
the requester owns on a pending request (stops, passengers, pickup, lead notes) and leaves
what the agent owns (vehicle and pricing) alone.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.conf import settings

from apps.addresses.models import Airline, Airport
from apps.contacts.models import Company, Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.dispatch import farm_in
from apps.leads.models import Lead
from apps.reservations.flights import link_flights
from apps.reservations.models import Passenger, Reservation, Stop

if TYPE_CHECKING:
    from apps.dispatch.farm_in import Summary

_TEXT_MAX = 255
_COORD = Decimal("0.000001")
_MAX_HEADCOUNT = 32767
_RATE_CARD_FIELDS = ["trip_type", "vehicle", "rate", "min_hours", "hours"]


def build(payload: dict, company: Company, *, summary: Summary) -> tuple[Lead, Reservation]:
    """A new GNet lead on the company's contact, with one trip priced from the rate card.

    The lead is not booked: accepting the request books it. A mapped vehicle snapshots its
    rate and the run type's minimum hours, as the reservation editor does; an unmapped one
    leaves the trip with no vehicle and no price for the agent to fill.
    """
    contact = company.contacts.order_by("pk").first() or Contact.objects.create(
        name=company.name, company=company, channel=Channel.GNET
    )
    lead = Lead.objects.create(
        contact=contact,
        channel=Channel.GNET,
        status=Lead.Status.NEW,
        notes=_lead_notes(payload, _res_no(payload)),
    )
    pickup_date, pickup_time = _local_pickup(summary.pickup_at, summary.timezone)
    reservation = Reservation(
        lead=lead,
        pickup_date=pickup_date,
        pickup_time=pickup_time,
        pickup_timezone=summary.timezone,
        passengers=_headcount(payload),
        rate=0,
        min_hours=0,
        hours=0,
        gratuity_pct=0,
        gratuity_flat=0,
        discount_pct=0,
        discount_flat=0,
    )
    _apply_rate_card(reservation, summary)
    reservation.save()
    _write_stops_and_passengers(reservation, payload, summary)
    return lead, reservation


def apply_update(reservation: Reservation, lead: Lead, payload: dict, *, summary: Summary) -> None:
    """Rewrite the requester's side of a pending request's order from a new payload.

    Stops and named passengers are deleted and rebuilt; the headcount, pickup date, time
    and zone and the lead notes are rewritten. Vehicle, rate, hours, gratuity and discount
    stay as the agent left them, except that a trip still without a vehicle takes the
    newly mapped one and its rate card.

    The trip row is re-read under a lock first (the caller holds the transaction), so an
    editor save on the same trip waits for this rewrite instead of interleaving with it.
    """
    reservation.refresh_from_db(from_queryset=Reservation.objects.select_for_update(of=("self",)))
    lead.notes = _lead_notes(payload, _res_no(payload))
    lead.save(update_fields=["notes", "updated_at"])
    reservation.pickup_date, reservation.pickup_time = _local_pickup(
        summary.pickup_at, summary.timezone
    )
    reservation.pickup_timezone = summary.timezone
    reservation.passengers = _headcount(payload)
    fields = ["pickup_date", "pickup_time", "pickup_timezone", "passengers", "updated_at"]
    if reservation.vehicle_id is None and summary.vehicle is not None:
        _apply_rate_card(reservation, summary)
        fields += _RATE_CARD_FIELDS
    reservation.save(update_fields=fields)
    reservation.stops.all().delete()
    reservation.named_passengers.all().delete()
    _write_stops_and_passengers(reservation, payload, summary)


def _res_no(payload: dict) -> str:
    return str(farm_in._dict(payload.get("affiliateReservation")).get("requesterResNo") or "")


def _apply_rate_card(reservation: Reservation, summary: Summary) -> None:
    """Snapshot the mapped vehicle's rate and minimum hours onto the trip (not saved).

    Writes exactly `_RATE_CARD_FIELDS`, which is what `apply_update` saves after it.
    """
    vehicle = summary.vehicle
    reservation.trip_type = (
        Reservation.TripType.HOURLY if summary.hourly else Reservation.TripType.TRANSFER
    )
    if vehicle is None:
        return
    reservation.vehicle = vehicle
    reservation.rate = vehicle.rate
    reservation.min_hours = (
        vehicle.hourly_min_hours if summary.hourly else vehicle.transfer_min_hours
    )
    reservation.hours = 0


def _write_stops_and_passengers(reservation: Reservation, payload: dict, summary: Summary) -> None:
    """Create the trip's stops and named passengers, then let the pickup stop set the zone."""
    stops = _stop_dicts(payload)
    link_flights(stops, reservation.pickup_date)
    Stop.objects.bulk_create(
        Stop(
            reservation=reservation,
            sequence=i,
            address=s["address"],
            note=s["note"],
            name=s["name"],
            latitude=s["lat"],
            longitude=s["lng"],
            airport_id=s["airport_id"],
            airline_id=s["airline_id"],
            flight_number=s["flight_number"],
            flight_direction=s["flight_direction"],
            flight_id=s["flight_id"],
        )
        for i, s in enumerate(stops)
    )
    _settle_pickup_zone(reservation, summary.pickup_at, summary.timezone)
    Passenger.objects.bulk_create(
        Passenger(reservation=reservation, **row) for row in _passenger_rows(payload)
    )


def _headcount(payload: dict) -> int:
    """Passengers on the trip from GNet's `passengerCount` string.

    Cosmetic, so junk falls back to the named passengers' count; the floor is 1 and an
    absurd count is capped rather than overflowing the column.
    """
    passengers = payload.get("passengers")
    fallback = len(passengers) if isinstance(passengers, list) else 0
    try:
        count = int(str(payload.get("passengerCount")).strip())
    except ValueError:
        count = fallback
    return min(max(count, 1), _MAX_HEADCOUNT)


def _local_pickup(at: datetime | None, zone: str) -> tuple[date | None, time | None]:
    """The pickup instant as a local date and minute-precision time in `zone` (or the default)."""
    if at is None:
        return None, None
    local = at.astimezone(ZoneInfo(zone or settings.TIME_ZONE))
    return local.date(), local.time().replace(second=0, microsecond=0)


def _settle_pickup_zone(reservation: Reservation, pickup_at: datetime | None, zone: str) -> None:
    """Let the saved pickup stop decide the trip's zone without moving the pickup instant.

    A pickup with no airport and no coordinate resolves to "", which would blank the zone,
    so the request's own `zone` stays. When the stop resolves a different zone, the local
    date and time are re-derived from the same UTC instant in that zone.
    """
    reservation.refresh_pickup_timezone()
    settled = reservation.pickup_timezone or zone
    if settled == zone and reservation.pickup_timezone:
        return
    reservation.pickup_timezone = settled
    reservation.pickup_date, reservation.pickup_time = _local_pickup(pickup_at, settled)
    reservation.save(update_fields=["pickup_timezone", "pickup_date", "pickup_time"])


def _lead_notes(payload: dict, res_no: str) -> str:
    """AFFILIATE notes, then the event name, then GNet's reservation number, one per line."""
    lines = _note_texts(payload, "AFFILIATE")
    event = str(payload.get("eventName") or "").strip()
    if event:
        lines.append(f"Event: {event}")
    lines.append(f"GNet res no {res_no}")
    return "\n".join(lines)


def _note_texts(payload: dict, context: str) -> list[str]:
    """Non-blank texts of the payload's `notes` addressed to `context` (AFFILIATE, DRIVER)."""
    notes = payload.get("notes")
    out = []
    for raw in notes if isinstance(notes, list) else []:
        note = farm_in._dict(raw)
        text = str(note.get("text") or "").strip()
        if text and str(note.get("context") or "").upper() == context:
            out.append(text)
    return out


def _coordinate(raw: object) -> Decimal | None:
    """A stop coordinate as a 6-place Decimal; None when absent, junk or out of range."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) > 180:
        return None
    return value.quantize(_COORD, ROUND_HALF_UP)


def _stop_number(location: dict, index: int) -> int:
    try:
        return int(str(location.get("stopNumber")).strip())
    except ValueError:
        return index


def _stop_dicts(payload: dict) -> list[dict]:
    """Pickup, the middle stops (by `stopNumber`, else as listed), dropoff.

    The pickup is always stop 0, even with a blank address, so its sign, notes and zone are
    never handed to the next stop; blank middle stops and a blank dropoff are skipped. An
    airport location named by IATA code resolves to our airport row and carries its flight;
    the first stop with an airport is the arrival side, the last the departure.
    """
    locations = farm_in._dict(payload.get("locations"))
    pickup = farm_in._dict(locations.get("pickup"))
    middle = locations.get("stops")
    middle = [farm_in._dict(s) for s in middle] if isinstance(middle, list) else []
    middle = [
        s for _, s in sorted(enumerate(middle), key=lambda p: (_stop_number(p[1], p[0]), p[0]))
    ]
    rest = [*middle, farm_in._dict(locations.get("dropOff"))]
    rest = [loc for loc in rest if str(loc.get("address") or "").strip()]
    airports, airlines = _airport_lookups([pickup, *rest])

    pickup_note = [
        str(payload.get("specialInstructions") or "").strip(),
        str(pickup.get("specialInstructions") or "").strip(),
        *_note_texts(payload, "DRIVER"),
    ]
    if str(pickup.get("meetAndGreet") or "").strip().lower() == "true":
        pickup_note.append("Meet and greet")
    first = _stop_dict(pickup, airports, airlines)
    first["name"] = str(payload.get("namesignInstructions") or "").strip()[:160]
    first["note"] = " · ".join(n for n in pickup_note if n)[:_TEXT_MAX]
    stops = [first, *(_stop_dict(loc, airports, airlines) for loc in rest)]

    last = len(stops) - 1
    for i, stop in enumerate(stops):
        if stop["airport_id"] and i == 0:
            stop["flight_direction"] = "arrival"
        elif stop["airport_id"] and i == last:
            stop["flight_direction"] = "departure"
    return stops


def _is_airport(location: dict) -> bool:
    return str(location.get("locationType") or "").lower() == "airport"


def _airport_lookups(locations: list[dict]) -> tuple[dict[str, Airport], dict[str, Airline]]:
    """Every airport and airline the locations name, fetched in one query each.

    Airports are keyed by IATA with the lowest pk winning, as `.first()` would pick.
    """
    airport_codes, airline_codes = set(), set()
    for location in filter(_is_airport, locations):
        airport_codes.add(str(location.get("address") or "").strip().upper())
        flight = farm_in._dict(location.get("flightInfo"))
        airline_codes.add(str(flight.get("airlineCode") or "").strip().upper())
    airport_codes.discard("")
    airline_codes.discard("")
    airports: dict[str, Airport] = {}
    if airport_codes:
        for airport in Airport.objects.filter(iata__in=airport_codes).order_by("pk"):
            airports.setdefault(airport.iata, airport)
    airlines = (
        {a.iata: a for a in Airline.objects.filter(iata__in=airline_codes)} if airline_codes else {}
    )
    return airports, airlines


def _stop_dict(location: dict, airports: dict[str, Airport], airlines: dict[str, Airline]) -> dict:
    """One GNet location as the stop dict `link_flights` reads."""
    address = str(location.get("address") or "").strip()
    stop = {
        "address": address[:_TEXT_MAX],
        "note": str(location.get("specialInstructions") or "").strip()[:_TEXT_MAX],
        "name": "",
        "lat": _coordinate(location.get("lat")),
        "lng": _coordinate(location.get("lon")),
        "airport_id": None,
        "airline_id": None,
        "flight_number": "",
        "flight_direction": "",
    }
    airport = airports.get(address.upper()) if _is_airport(location) else None
    if airport is None:
        return stop
    flight = farm_in._dict(location.get("flightInfo"))
    airline = airlines.get(str(flight.get("airlineCode") or "").strip().upper())
    stop.update(
        address=f"{airport.name} ({airport.iata})"[:_TEXT_MAX],
        airport_id=airport.pk,
        airline_id=airline.pk if airline else None,
        flight_number="".join(
            c for c in str(flight.get("flightNumber") or "") if c in "0123456789"
        )[:6],
    )
    return stop


def _passenger_rows(payload: dict) -> list[dict]:
    """Named riders with E.164 mobiles; the first is the lead when it has a mobile."""
    passengers = payload.get("passengers")
    rows: list[dict] = []
    for raw in passengers if isinstance(passengers, list) else []:
        raw = farm_in._dict(raw)
        name = f"{raw.get('firstName') or ''} {raw.get('lastName') or ''}".strip()[:120]
        if not name:
            continue
        phone = raw.get("phoneNumber")
        if not phone:
            phones = raw.get("phones")
            first = farm_in._dict(phones[0]) if isinstance(phones, list) and phones else {}
            phone = first.get("number")
        mobile = to_e164(str(phone) if phone else "") or ""
        rows.append(
            {
                "name": name,
                "mobile": mobile,
                "is_lead": not rows and bool(mobile),
                "sort": len(rows),
            }
        )
    return rows
