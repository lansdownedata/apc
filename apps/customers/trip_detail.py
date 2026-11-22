"""One trip's page (spec §6.3, mockup 3): where it stands, the itinerary, the driver once
dispatched, and its money. `trip_detail` hands the template a plain dict built from named
fields only: no cost, affiliate, staff note or other internal field is ever read into it.

Notes. The only note a customer sees is a stop's `Stop.note`: the itinerary note the driver
reads on the driver link ("20-min photo stop"). Everything else that holds notes is the
office's own working text and is never read here: `Lead.notes` (agents edit it freely;
intake text lands in it too), `Lead.lost_reason`, `Reservation.cancel_reason` and
`driver_no_show_note`, the assignment's `note` and `sent_back_reason`, `Charge.note`,
`Contact.notes`, and the trip review's notes and issues.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.db.models.query import prefetch_related_objects
from django.utils import timezone

from apps.dispatch.selectors import coverage_prefetch
from apps.leads.models import Lead
from apps.payments.models import Charge
from apps.planners.selectors import DISPATCHED_STATUSES
from apps.planners.selectors import _driver as planner_driver
from apps.public.wedding import format_clock
from apps.reservations.models import FlightDirection, Reservation, Stop, TripStatusEvent

from . import receipts, requests, selectors
from .models import CustomerAccount

_S = Reservation.TripStatus

REQUESTED, QUOTE_SENT, DEPOSIT_PAID, CONFIRMED, DRIVER_ASSIGNED, COMPLETED = STEPS = (
    "Requested",
    "Quote sent",
    "Deposit paid",
    "Confirmed",
    "Driver assigned",
    "Completed",
)
# Where each live label stands on the progress line.
_POSITION = {
    "request_received": REQUESTED,
    "quote_ready": QUOTE_SENT,
    "quote_expired": QUOTE_SENT,
    "confirming": DEPOSIT_PAID,
    "confirmed": CONFIRMED,
    "driver_assigned": DRIVER_ASSIGNED,
    "in_progress": DRIVER_ASSIGNED,
    "completed": COMPLETED,
}
# A step a trip can pass without it happening (a phone booking sends no quote and may take
# no deposit): once passed with no record it is left out, never claimed.
_SKIPPABLE = frozenset({QUOTE_SENT, DEPOSIT_PAID})
# Labels that end the line: the steps reached, then the label itself.
_ENDS = frozenset({"cancelled", "no_show", "closed"})
# Trips still on their way to a driver show where the driver will appear.
_AWAITING_DRIVER = frozenset(
    {"request_received", "quote_ready", "confirming", "confirmed", "driver_assigned"}
)
DRIVER_PENDING = "Your driver's name and cell appear here once assigned."


def _zone(trip: Reservation) -> ZoneInfo:
    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def _deposit_at(lead: Lead) -> datetime | None:
    """When the order's first succeeded or authorized deposit was paid: the hold placed,
    else the capture, else (a charge that never went through manual capture) when the row
    last changed. None for an order that paid none. One query."""
    if lead.on_account:
        return None
    row = (
        Charge.objects.filter(
            plan__lead=lead,
            kind=Charge.Kind.DEPOSIT,
            status__in=(Charge.Status.SUCCEEDED, Charge.Status.AUTHORIZED),
        )
        .order_by("created_at", "pk")
        .values_list("authorized_at", "captured_at", "updated_at")
        .first()
    )
    return next((at for at in row if at), None) if row else None


def _stamps(trip: Reservation) -> dict[str, datetime]:
    """When each step happened, where there is a record of it (one query for the status
    history, one for the deposit).

    Confirmed: booking (`leads.services.book_lead`) stamps nothing on the lead itself, but
    it moves every pending trip to Unassigned through `set_trip_status`, which logs a
    `TripStatusEvent` (source System). The order's earliest such event is the moment it
    booked; failing one (a trip created already booked), the payment plan's creation."""
    lead = trip.lead
    events = TripStatusEvent.objects.filter(
        Q(reservation_id=trip.pk)
        | Q(
            reservation__lead_id=lead.pk, status=_S.UNASSIGNED, source=TripStatusEvent.Source.SYSTEM
        )
    ).order_by("created_at", "pk")
    stamps: dict[str, datetime] = {REQUESTED: lead.created_at}
    if lead.quote_sent_at:
        stamps[QUOTE_SENT] = lead.quote_sent_at
    deposit = _deposit_at(lead)
    if deposit:
        stamps[DEPOSIT_PAID] = deposit
    for trip_id, status, source, at in events.values_list(
        "reservation_id", "status", "source", "created_at"
    ):
        if status == _S.UNASSIGNED and source == TripStatusEvent.Source.SYSTEM:
            stamps.setdefault(CONFIRMED, at)
        if trip_id != trip.pk:
            continue
        if status in DISPATCHED_STATUSES:
            stamps.setdefault(DRIVER_ASSIGNED, at)
        if status == _S.DONE:
            stamps.setdefault(COMPLETED, at)
        if status == _S.NO_SHOW:
            stamps.setdefault("no_show", at)
        if status in receipts.CANCELLED:
            stamps.setdefault("cancelled", at)
    plan = getattr(lead, "payment", None)
    if CONFIRMED not in stamps and plan is not None and _booked(trip):
        stamps[CONFIRMED] = plan.created_at
    if trip.cancelled_at:
        stamps["cancelled"] = trip.cancelled_at
    return stamps


def _booked(trip: Reservation) -> bool:
    """The order booked: it is Booked, or booking froze the trip's total before it was
    lost (`selectors.label_key`)."""
    return trip.lead.status == Lead.Status.BOOKED or trip.booked_total is not None


def _step(trip: Reservation, name: str, at: datetime | None, state: str) -> dict:
    date = selectors.short_day(at, trip) if at and state != "todo" else ""
    return {"name": name, "date": date, "state": state}


def _steps(trip: Reservation, key: str, stamps: dict[str, datetime]) -> list[dict]:
    """The progress line (spec §5). A live trip: every step up to where it stands is done,
    that one is `now`, the rest `todo`; a skippable step passed with no record is left out.
    A cancelled, no-show or closed trip: the steps it reached, then its label. On-account
    orders have no deposit step. A step that has not happened carries no date."""
    names = [n for n in STEPS if not (n == DEPOSIT_PAID and trip.lead.on_account)]
    if key in _ENDS:
        reached = [
            _step(trip, name, stamps.get(name), "done")
            for name in names
            if name in stamps or (name == CONFIRMED and _booked(trip))
        ]
        end = selectors.LABELS[key][0]
        return [*reached, _step(trip, end, stamps.get(key), "now")]
    at = STEPS.index(_POSITION[key])
    steps = []
    for name in names:
        i = STEPS.index(name)
        if i < at and name in _SKIPPABLE and name not in stamps:
            continue
        state = "done" if i < at else "now" if i == at else "todo"
        steps.append(_step(trip, name, stamps.get(name), state))
    return steps


def _title(stop: Stop) -> str:
    """An airport as "Reagan National Airport (DCA)", else the stop's name or address."""
    if stop.airport_id:
        airport = stop.airport
        title = stop.name.strip() or airport.name
        code = airport.iata
        return f"{title} ({code})" if code and f"({code})" not in title else title
    return stop.name.strip() or stop.address.strip() or "Location to be confirmed"


def _flight(stop: Stop, trip: Reservation, first: bool, last: bool) -> str:
    """ "American Airlines 2114, arriving 8:52 AM": the customer's rendering of the flight
    (`Stop.flight_label_long`), with the cached time while the flight is still expected,
    in the airport's zone (its abbreviation added when that is not the trip's)."""
    label = stop.flight_label_long if (stop.airline_id or stop.flight_number) else ""
    if not label:
        return ""
    direction = stop.flight_direction or (
        FlightDirection.ARRIVAL if first else FlightDirection.DEPARTURE if last else ""
    )
    verb = {FlightDirection.ARRIVAL: "arriving", FlightDirection.DEPARTURE: "departing"}.get(
        direction, ""
    )
    at = selectors.expected_flight_at(stop.flight)
    if not (verb and at):
        return label
    airport_zone = stop.airport.timezone if stop.airport_id else ""
    local = timezone.localtime(at, ZoneInfo(airport_zone) if airport_zone else _zone(trip))
    clock = format_clock(local.time())
    if local.tzname() != trip.pickup_tz_label:
        clock = f"{clock} {local.tzname()}"
    return f"{label}, {verb} {clock}"


def _stops(trip: Reservation) -> list[Stop]:
    """The trip's stops with their airport, airline and cached flight: one query."""
    return list(
        Stop.objects.filter(reservation_id=trip.pk)
        .select_related("airport", "airline", "flight")
        .order_by("sequence", "pk")
    )


def _itinerary(trip: Reservation, stops: list[Stop], zone_label: str) -> list[dict]:
    """`{"title", "address", "detail"}` per stop. Pickup carries the pickup time with the
    trip's zone, a middle stop its own scheduled time, the last stop reads "Drop-off"; a
    flight line follows where the stop has one. `address` is set only when the title is a
    place's name rather than its address."""
    out = []
    for i, stop in enumerate(stops):
        first, last = i == 0, i == len(stops) - 1 and i > 0
        if first:
            role = "Pickup"
            clock = trip.pickup_time
        elif last:
            role, clock = "Drop-off", None
        else:
            role, clock = "Stop", stop.scheduled_time
        parts = [f"{role} {format_clock(clock)} {zone_label}".strip() if clock else role]
        flight = _flight(stop, trip, first, last)
        if flight:
            parts.append(flight)
        title = _title(stop)
        address = stop.address.strip()
        detail = ". ".join(parts)
        out.append(
            {
                "title": title,
                "address": address if address and address != title and not stop.airport_id else "",
                "detail": f"{detail}." if len(parts) > 1 or clock else detail,
            }
        )
    return out


def _notes(stops: list[Stop]) -> str:
    """The stops' notes, one a line; each named by its stop when more than one has one."""
    noted = [(stop, stop.note.strip()) for stop in stops if stop.note.strip()]
    if len(noted) == 1:
        return noted[0][1]
    return "\n".join(f"{_title(stop)}: {note}" for stop, note in noted)


def _driver(trip: Reservation) -> dict | None:
    """Name and cell from Dispatched on (`planners.selectors._driver`): never the
    affiliate, and never the vehicle the affiliate described."""
    if trip.trip_status not in DISPATCHED_STATUSES:
        return None
    prefetch_related_objects([trip], coverage_prefetch())
    info = planner_driver(trip)
    return {"name": info["name"], "cell": info["cell"]} if info else None


def _line(label: str, amount: Decimal) -> dict:
    """A charges line; `abs_amount` so a credit renders as "−$20.00", as on the quote page."""
    return {"label": label, "amount": amount, "abs_amount": abs(amount)}


def _charges(trip: Reservation, total: Decimal | None) -> list[dict]:
    """The charges card's lines. A cancelled trip has one, "Cancellation charge", for what
    was kept (spec §7, the receipt's wording); an unpriced trip none."""
    if total is None:
        return []
    if trip.trip_status in receipts.CANCELLED:
        return [_line("Cancellation charge", total)] if total > 0 else []
    return [_line(line.label, line.amount) for line in trip.price_lines()]


def _paid_line(trip: Reservation, total: Decimal | None, paid: Decimal) -> str:
    """From the trip's own money (`selectors.paid_amounts`, the list's figure): paid in
    full, part paid (its share of the deposit), or billed to the account. Blank for an
    unpriced, unpaid or cancelled trip."""
    if total is None or trip.trip_status in receipts.CANCELLED:
        return ""
    if trip.lead.on_account:
        return "Billed to your account"
    if total > 0 and paid >= total:
        return "Paid in full"
    return "Deposit paid" if paid > 0 else ""


def _pay(trip: Reservation, key: str) -> tuple[str | None, str]:
    """Home's pay link for this trip's order (`selectors.owing`, `selectors.pay_action`):
    a quote ready to review, or money a booked order owes."""
    lead = trip.lead
    owed = selectors.owing([lead]).get(lead.pk) if lead.status == Lead.Status.BOOKED else None
    if key != "quote_ready" and owed is None:
        return None, ""
    return selectors.pay_action(lead, owed)


def _until_pickup(trip: Reservation) -> str:
    """ "Your pickup is 12 days away." for the cancel dialog: whole days between today and
    the pickup day in the trip's own zone, or hours once under two days (a trip with no
    time stays in days). Blank without a date or once the pickup has passed."""
    at, now = trip.pickup_at, timezone.now()
    if at is None or at <= now:
        return ""
    left = at - now
    if trip.pickup_time is not None and left < timedelta(days=2):
        hours = int(left.total_seconds() // 3600)
        if hours < 1:
            return "Your pickup is less than an hour away."
        return f"Your pickup is {hours} hour{'s' if hours != 1 else ''} away."
    days = (trip.pickup_date - timezone.localdate(now, timezone=_zone(trip))).days
    return "Your pickup is tomorrow." if days == 1 else f"Your pickup is {days} days away."


def _requests(account: CustomerAccount, trip: Reservation, key: str, route: str) -> dict:
    """What the change and cancel dialogs need (`can_request`, `ask_when`, `pickup_in`),
    `call_dispatch` (a Quote ready or Confirming trip still ahead: it shows "Need to change
    or cancel this? Call dispatch" in place of the buttons), and
    the open requests: `pending_request`, the newest (`{"kind", "date"}` or None), and
    `pending_change` / `pending_cancel`, the date each kind was last asked for while its
    task is open, so one pending kind never hides the other's button. One query, only
    while the trip takes requests."""
    if not requests.can_ask(trip, key):
        at = trip.pickup_at
        return {
            "can_request": False,
            "call_dispatch": key in requests.CALL_TO_ASK and (at is None or at > timezone.now()),
            "ask_when": "",
            "pickup_in": "",
            "pending_request": None,
            "pending_change": "",
            "pending_cancel": "",
        }
    open_requests = requests.pending(account, trip)
    latest: dict[str, str] = {}
    for row in reversed(open_requests):
        latest[row["kind"]] = row["date"]
    return {
        "can_request": True,
        "call_dispatch": False,
        "ask_when": f"{requests.when(trip)} · {route}",
        "pickup_in": _until_pickup(trip),
        "pending_request": open_requests[0] if open_requests else None,
        "pending_change": latest.get("change", ""),
        "pending_cancel": latest.get("cancel", ""),
    }


def trip_detail(account: CustomerAccount, trip: Reservation) -> dict:
    """The trip page (spec §6.3). `trip` comes from `selectors.trip_or_404(account, pk)`.

    The `selectors.trip_rows` row's keys, plus: `heading`, `heading_zone` and `when_note`
    (`selectors.heading`); `steps` (`{"name", "date", "state"}`, state `done|now|todo`);
    `stops` (`{"title", "address", "detail"}`);
    `vehicle`; `passengers` (names) and `passenger_count`; `driver` (`{"name", "cell"}`
    or None) and `driver_pending` (the line shown in its place, or blank); `notes`;
    `price_lines` (`{"label", "amount", "abs_amount"}`); `total` (None while unpriced); `paid_line`;
    `pay_url` and `pay_label`; `receipt_pending` (waiting for billing to process it);
    `can_request` and the rest of `_requests` (the change and cancel dialogs).

    At most 30 queries whatever the trip holds: the scoped trip, the row's money, the
    stops, the status history, the deposit, the driver from Dispatched on, and the pay
    page's ledger reads for a booked order that may owe, and the open requests."""
    # The order's trips: the money split, the pay page and `quote_total` all read them.
    prefetch_related_objects([trip.lead], "reservations")
    paid = selectors.paid_amounts([trip])
    row = selectors.trip_rows([trip], paid)[0]
    key = selectors.label_key(trip)
    stops = _stops(trip)
    total = row["total"]
    driver = _driver(trip)
    pay_url, pay_label = _pay(trip, key)
    return {
        **row,
        **selectors.heading(trip, row),
        "steps": _steps(trip, key, _stamps(trip)),
        "stops": _itinerary(trip, stops, row["zone"]),
        "vehicle": trip.vehicle.name if trip.vehicle_id else "Vehicle to be confirmed",
        "passengers": receipts.passenger_names(trip),
        "passenger_count": trip.passengers,
        "driver": driver,
        "driver_pending": DRIVER_PENDING if driver is None and key in _AWAITING_DRIVER else "",
        "notes": _notes(stops),
        "price_lines": _charges(trip, total),
        "total": total,
        "paid_line": _paid_line(trip, total, paid.get(trip.pk, Decimal("0.00"))),
        "pay_url": pay_url,
        "pay_label": pay_label,
        "receipt_pending": receipts.awaiting_processing(trip),
        **_requests(account, trip, key, row["route"]),
    }
