"""Desk actions on a pending farm-in request: price, vehicle, coverage, send back, decide.

Every action locks the request row first (`_locked`), then checks it is still pending, then
acts, so two staff deciding at once cannot both win. Callers see `FarmInActionError` with an
HTTP-ish `status`: 409 when someone already decided, 400 for any other refusal.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Department, User
from apps.addresses.models import Airline, Airport
from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.core.phone import to_e164
from apps.dispatch import farm_in, farm_in_push
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.leads.models import Lead, VehicleType
from apps.leads.services import BookLeadError, book_lead
from apps.notifications.models import Notification
from apps.reservations.flights import link_flights
from apps.reservations.models import Passenger, Reservation, Stop

logger = logging.getLogger(__name__)

_DECIDED = (FarmInRequest.Status.ACCEPTED, FarmInRequest.Status.DECLINED)
_TEXT_MAX = 255


class FarmInActionError(Exception):
    """A desk action was refused; `status` is the HTTP status the view should answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _locked(pk: int) -> FarmInRequest:
    """Fetch the request under a row lock; call inside `transaction.atomic()`."""
    return FarmInRequest.objects.select_for_update().select_related("company", "vehicle").get(pk=pk)


def _require_pending(req: FarmInRequest) -> None:
    if req.status in _DECIDED:
        raise FarmInActionError("Already decided", status=409)
    if req.status != FarmInRequest.Status.PENDING:
        raise FarmInActionError(f"Request is {req.get_status_display().lower()}")


def _notify_quietly(
    kind: str, title: str, detail: str, req_pk: int, user: User | None = None
) -> None:
    """Create one notification; a failure is logged, never raised into the action."""
    try:
        Notification.objects.create(
            lead=None,
            kind=kind,
            title=title[:160],
            detail=detail[:_TEXT_MAX],
            user=user,
            farm_in_request_id=req_pk,
        )
    except Exception:
        logger.exception("farm-in: %s notification failed for request %s", kind, req_pk)


def price(pk: int, *, user: User, amount: Decimal, note: str = "") -> FarmInRequest:
    """Set a manual price; it survives later vehicle changes and requester updates."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if amount is None or amount <= 0:
            raise FarmInActionError("Enter a price above zero")
        req.quoted_amount = amount
        req.pricing_source = FarmInRequest.PricingSource.MANUAL
        req.priced_by = user
        req.pricing_note = note[:_TEXT_MAX]
        req.save()
    return req


def set_vehicle(pk: int, *, user: User, vehicle_id: int) -> FarmInRequest:
    """Map the request to one of our vehicle types and re-price it unless priced by hand."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        vehicle = VehicleType.objects.filter(pk=vehicle_id, active=True).first()
        if vehicle is None:
            raise FarmInActionError("Unknown vehicle")
        req.vehicle = vehicle
        if req.pricing_source != FarmInRequest.PricingSource.MANUAL:
            req.quoted_amount = farm_in.price_request(
                vehicle, hourly=farm_in.is_hourly(req.payload)
            )
            req.pricing_source = (
                FarmInRequest.PricingSource.RATE_CARD if req.quoted_amount is not None else ""
            )
        unmapped = farm_in.ALERT_UNMAPPED.format(code=req.gnet_vehicle_type)
        req.alert = " · ".join(a for a in req.alert.split(" · ") if a and a != unmapped)
        req.save()
    return req


def request_coverage(pk: int, *, user: User) -> FarmInRequest:
    """Hand the request to dispatch and tell the operations team."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if req.desk != FarmInRequest.Desk.RESERVATIONS:
            raise FarmInActionError("Already with dispatch")
        req.desk = FarmInRequest.Desk.DISPATCH
        req.desk_changed_by = user
        req.desk_changed_at = timezone.now()
        req.save()
        title = f"Coverage requested: {req.company.name}"
        detail = f"{req.pickup_summary} → {req.dropoff_summary}"
        recipients = list(User.objects.in_department(Department.OPERATIONS).filter(is_active=True))
        recipients = recipients or [None]
        for recipient in recipients:
            transaction.on_commit(
                lambda r=recipient: _notify_quietly(
                    Notification.Kind.FARM_IN_COVERAGE, title, detail, pk, r
                )
            )
    return req


def send_back(pk: int, *, user: User, note: str) -> FarmInRequest:
    """Return the request to reservations with a reason, telling whoever asked for coverage."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if req.desk != FarmInRequest.Desk.DISPATCH:
            raise FarmInActionError("Already with reservations")
        note = note.strip()
        if not note:
            raise FarmInActionError("Say why it is going back")
        previous = req.desk_changed_by
        if previous is not None and not previous.is_active:
            previous = None
        req.desk = FarmInRequest.Desk.RESERVATIONS
        req.desk_note = note[:_TEXT_MAX]
        req.desk_changed_by = user
        req.desk_changed_at = timezone.now()
        req.save()
        title = f"Sent back: {req.company.name}"
        transaction.on_commit(
            lambda: _notify_quietly(
                Notification.Kind.FARM_IN_SENT_BACK, title, note[:_TEXT_MAX], pk, previous
            )
        )
    return req


def decline(pk: int, *, user: User, reason: str) -> FarmInRequest:
    """Decline the request and queue the REJECT push to GNet."""
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        reason = reason.strip()
        if not reason:
            raise FarmInActionError("Give the requester a reason")
        req.status = FarmInRequest.Status.DECLINED
        req.decline_reason = reason[:_TEXT_MAX]
        req.decided_by = user
        req.decided_at = timezone.now()
        req.save()
        farm_in_push.enqueue(req, "REJECT")
    return req


ALERT_ACCEPTED_NO_PICKUP = "No pickup time: set it on the order"
_CENT = Decimal("0.01")
_COORD = Decimal("0.000001")


def accept(pk: int, *, user: User) -> FarmInRequest:
    """Book the request as an order and queue the CONFIRMED push with the trip's total.

    The order is built inside the same transaction that holds the request lock, so a
    refused booking leaves no lead behind and the request stays pending.
    """
    with transaction.atomic():
        req = _locked(pk)
        _require_pending(req)
        if req.quoted_amount is None:
            raise FarmInActionError("Price the request first")
        if req.vehicle is None:
            raise FarmInActionError("Choose a vehicle first")
        lead, reservation = _build_order(req, user=user)
        req.status = FarmInRequest.Status.ACCEPTED
        req.lead = lead
        req.reservation = reservation
        req.decided_by = user
        req.decided_at = timezone.now()
        req.alert = ALERT_ACCEPTED_NO_PICKUP if req.pickup_at is None else ""
        req.save()
        farm_in_push.enqueue(req, "CONFIRMED", reservation.line_total)
    return req


def _build_order(req: FarmInRequest, *, user: User) -> tuple[Lead, Reservation]:
    """Turn a priced, vehicle-mapped request into a booked lead with one trip.

    The trip is priced so its `line_total` is the quote: rate = quote / billed hours, where
    billed hours is the vehicle's rate-card minimum (or a 1-hour override when the vehicle
    has none). Rounding the rate can move the total by a cent, which is why `accept` pushes
    `line_total` rather than the quote.
    """
    payload = req.payload
    vehicle = req.vehicle
    company = req.company
    contact = company.contacts.order_by("pk").first() or Contact.objects.create(
        name=company.name, company=company, channel=Channel.GNET
    )
    lead = Lead.objects.create(
        contact=contact,
        channel=Channel.GNET,
        status=Lead.Status.NEW,
        notes=_lead_notes(payload, req.requester_res_no),
    )

    hourly = farm_in.is_hourly(payload)
    min_hours = Decimal(vehicle.hourly_min_hours if hourly else vehicle.transfer_min_hours)
    if min_hours <= 0:
        hours, min_hours = Decimal("1"), Decimal("0")
    else:
        hours = Decimal("0")
    billed = hours if hours > 0 else min_hours
    rate = (req.quoted_amount / billed).quantize(_CENT, ROUND_HALF_UP)

    local = req.pickup_at.astimezone(ZoneInfo(req.timezone)) if req.pickup_at else None
    reservation = Reservation.objects.create(
        lead=lead,
        vehicle=vehicle,
        trip_type=Reservation.TripType.HOURLY if hourly else Reservation.TripType.TRANSFER,
        pickup_date=local.date() if local else None,
        pickup_time=local.time() if local else None,
        pickup_timezone=req.timezone,
        passengers=req.passenger_count or 1,
        rate=rate,
        min_hours=min_hours,
        hours=hours,
        gratuity_pct=0,
        gratuity_flat=0,
        discount_pct=0,
        discount_flat=0,
    )

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
    reservation.refresh_pickup_timezone()
    if not reservation.pickup_timezone and req.timezone:
        # No airport and no coordinate on the pickup: keep the zone the request resolved.
        reservation.pickup_timezone = req.timezone
        reservation.save(update_fields=["pickup_timezone"])

    Passenger.objects.bulk_create(
        Passenger(reservation=reservation, **row) for row in _passenger_rows(payload)
    )

    try:
        book_lead(lead)
    except BookLeadError as exc:
        raise FarmInActionError(str(exc)) from exc
    return lead, reservation


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
    """Pickup, the middle stops (by `stopNumber`, else as listed), dropoff; blanks skipped.

    An airport location named by IATA code resolves to our airport row and carries its
    flight; the first stop with an airport is the arrival side, the last the departure.
    """
    locations = farm_in._dict(payload.get("locations"))
    pickup = farm_in._dict(locations.get("pickup"))
    middle = locations.get("stops")
    middle = [farm_in._dict(s) for s in middle] if isinstance(middle, list) else []
    middle = [
        s for _, s in sorted(enumerate(middle), key=lambda p: (_stop_number(p[1], p[0]), p[0]))
    ]

    pickup_note = [str(payload.get("specialInstructions") or "").strip()]
    pickup_note += _note_texts(payload, "DRIVER")
    if str(pickup.get("meetAndGreet") or "").strip().lower() == "true":
        pickup_note.append("Meet and greet")
    pickup_name = str(payload.get("namesignInstructions") or "").strip()[:160]

    stops = []
    for location in [pickup, *middle, farm_in._dict(locations.get("dropOff"))]:
        stop = _stop_dict(location)
        if stop is None:
            continue
        if location is pickup:
            stop["name"] = pickup_name
            stop["note"] = " · ".join(n for n in pickup_note if n)[:_TEXT_MAX]
        stops.append(stop)

    last = len(stops) - 1
    for i, stop in enumerate(stops):
        if stop["airport_id"] and i == 0:
            stop["flight_direction"] = "arrival"
        elif stop["airport_id"] and i == last:
            stop["flight_direction"] = "departure"
    return stops


def _stop_dict(location: dict) -> dict | None:
    """One GNet location as the stop dict `link_flights` reads; None when it has no address."""
    address = str(location.get("address") or "").strip()
    if not address:
        return None
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
    if str(location.get("locationType") or "").lower() != "airport":
        return stop
    airport = Airport.objects.filter(iata=address.upper()).first()
    if airport is None:
        return stop
    flight = farm_in._dict(location.get("flightInfo"))
    code = str(flight.get("airlineCode") or "").strip().upper()
    airline = Airline.objects.filter(iata=code).first() if code else None
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


def retry_push(push_pk: int, *, user: User) -> FarmInStatusPush:
    """Queue a fresh push for a failed one; refuses when delivered or already queued."""
    with transaction.atomic():
        push = FarmInStatusPush.objects.get(pk=push_pk)
        _locked(push.request_id)
        push.refresh_from_db()
        try:
            return farm_in_push.retry(push)
        except ValueError as exc:
            raise FarmInActionError(str(exc)) from exc
