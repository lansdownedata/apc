"""What a signed-in customer changes or asks for from the portal.

Every write to an existing trip resolves it through `selectors.visible_trips(account)`, so
an id from another account is skipped, never written. `Reservation` is audited: each
changed trip is saved on its own with `update_fields`, so the trail records it under the
request's source (`customer portal`, `apps.audit.middleware`). A requested trip is a new
lead on the account's own contact (`request_trip`). A change or cancellation is only asked
for (`ask`): a message in the Inbox and a task for staff, and the trip stays as it is.
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
from apps.planners.services import RequestRefused
from apps.public.wedding import format_clock
from apps.reservations.flights import link_flights
from apps.reservations.models import Passenger, Reservation, Stop
from apps.reservations.passengers import NAME_MAX
from apps.tasks.models import Task

from .models import CustomerAccount, CustomerRequest
from .selectors import _short_day, label_key, visible_trips
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
NOTIFICATION_TITLE_MAX = Notification._meta.get_field("title").max_length
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
    """(name, mobile) of the trip's lead passenger: who trip-day texts go to. A contact's
    name can be longer than a passenger's, so it is cut as `reservations.passengers` cuts
    names."""
    if data.get("rider") == "other":
        return data["rider_name"], data["rider_mobile"]
    contact = account.contact
    return contact.name.strip()[:NAME_MAX], to_e164(contact.phone) or ""


def can_request(account: CustomerAccount) -> bool:
    """Whether the account may request trips here. Not when its contact's company is an
    affiliate account: the lead would be a farm-in (`Lead.is_farm_in`), which the portal
    never shows (`selectors.visible_trips`) and which skips holds, tasks and messages."""
    company = account.contact.company
    return not (company is not None and company.is_affiliate_account)


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
    two leads past either. Refused with `CustomerError("affiliate")`, before anything is
    written, when the account may not request trips (`can_request`)."""
    if not can_request(account):
        raise CustomerError("affiliate")
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
            title=f"New portal request: {contact.name}"[:NOTIFICATION_TITLE_MAX],
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


# --- ask for a change or a cancellation (spec §9) ----------------------------------------

CHANGE_TOPICS = (
    "Date or time",
    "Pickup",
    "Drop-off",
    "Passengers",
    "Vehicle",
    "Flight",
    "Something else",
)
CANCEL_REASONS = ("Plans changed", "Booked by mistake", "Found another option", "Other")
# The planner request's limits (`planners.services`): per account, per rolling hour.
ASK_LIMIT = 10
ASK_WINDOW = timedelta(hours=1)
ASK_MAX_LENGTH = 2000
# The same words from the same account about the same trip inside this window are a
# double tap on Send, not a second request.
ASK_DUPLICATE_WINDOW = timedelta(seconds=30)
# A change or cancellation may be asked for on these labels (`selectors.LABELS`): booked
# trips only. The request's task opens on the order, and the task cron retires every task
# on an order that is not booked (`tasks.jobs._sweep`), so it would be lost.
ASKABLE = frozenset({"confirmed", "driver_assigned"})
# Before booking these call dispatch instead (`trip_detail`, "Need to change or cancel this?").
CALL_TO_ASK = frozenset({"quote_ready", "confirming"})
_TASK_KIND = {
    CustomerRequest.Kind.CHANGE: "customer_change_request",
    CustomerRequest.Kind.CANCEL: "customer_cancel_request",
}


def can_ask(trip: Reservation, key: str | None = None) -> bool:
    """Whether the trip takes a change or cancellation request: Confirmed or Driver
    assigned, and its pickup (in the trip's own zone) still ahead. A trip with no date yet
    counts as ahead. `key` is `selectors.label_key(trip)` when the
    caller has it."""
    at = trip.pickup_at
    key = label_key(trip) if key is None else key
    return key in ASKABLE and (at is None or at > timezone.now())


def when(trip: Reservation) -> str:
    """ "Thu, Dec 3 at 9:10 AM EST": the pickup in the trip's own zone, as far as known."""
    day = trip.pickup_date
    if day is None:
        return "Date to be confirmed"
    text = f"{day:%a, %b} {day.day}"
    if trip.pickup_time:
        text = f"{text} at {format_clock(trip.pickup_time)} {trip.pickup_tz_label}"
    return text


def _composed(trip: Reservation, kind: str, topics: list[str], reason: str, text: str) -> str:
    """The Inbox message: what is asked, which trip and when, the choice, then their words."""
    if kind == CustomerRequest.Kind.CHANGE:
        head = f"Customer change request · {trip.reference} · {when(trip)}"
        choice = f"About: {', '.join(topics)}" if topics else ""
    else:
        head = f"Customer cancellation request · {trip.reference} · {when(trip)}"
        choice = f"Reason: {reason}"
    first = "\n".join(line for line in (head, choice) if line)
    return f"{first}\n\n{text}" if text else first


def ask(
    account: CustomerAccount,
    trip: Reservation,
    *,
    kind: str,
    topics: list[str],
    reason: str,
    body: str,
) -> CustomerRequest:
    """Record a customer's change (`kind="change"`) or cancellation (`"cancel"`) request
    about `trip` as an inbound message on the account's own conversation, and open a
    Customer Service task on the trip's order. The caller resolved `trip` through
    `selectors.visible_trips(account)`. Nothing on the trip or its order is changed.

    `topics` keeps only `CHANGE_TOPICS` (in their order); a reason outside
    `CANCEL_REASONS` is "Other". The body is required for a change and optional for a
    cancel, at most `ASK_MAX_LENGTH` characters. The same request again inside
    `ASK_DUPLICATE_WINDOW` returns the first. Raises `RequestRefused` with `closed` (the
    trip no longer takes requests, `can_ask`), `blank`, `too_long` or `limited` (past
    `ASK_LIMIT` in the rolling hour). An archived conversation stays archived: the
    message is still recorded and the task still opens, so the request can't be lost."""
    from apps.messaging import services as messaging
    from apps.messaging.models import Message
    from apps.tasks import services as tasks

    if kind not in _TASK_KIND:
        raise ValueError(f"unknown request kind {kind!r}")
    if not can_ask(trip):
        raise RequestRefused("closed")
    # A browser counts a line break as one character against `maxlength` but sends two.
    text = (body or "").replace("\r\n", "\n").strip()
    if kind == CustomerRequest.Kind.CHANGE and not text:
        raise RequestRefused("blank")
    if len(text) > ASK_MAX_LENGTH:
        raise RequestRefused("too_long")
    chosen = set(topics) if kind == CustomerRequest.Kind.CHANGE else set()
    topics = [topic for topic in CHANGE_TOPICS if topic in chosen]
    if kind == CustomerRequest.Kind.CANCEL:
        reason = reason if reason in CANCEL_REASONS else "Other"
    else:
        reason = ""
    composed = _composed(trip, kind, topics, reason, text)
    with transaction.atomic():
        # Serialise one account's submissions, so a double tap can't slip two rows past
        # the duplicate check or the limit.
        CustomerAccount.objects.select_for_update().filter(pk=account.pk).first()
        now = timezone.now()
        duplicate = (
            account.requests.filter(
                reservation=trip,
                message__body=composed,
                created_at__gte=now - ASK_DUPLICATE_WINDOW,
            )
            .order_by("-created_at", "-pk")
            .first()
        )
        if duplicate is not None:
            return duplicate
        if account.requests.filter(created_at__gte=now - ASK_WINDOW).count() >= ASK_LIMIT:
            raise RequestRefused("limited")
        message = messaging.record_inbound(
            messaging.conversation_for(account.contact),
            channel=Message.Channel.PORTAL,
            body=composed,
            sender_name=account.contact.name,
        )
        task = tasks.open_request_task(trip.lead, _TASK_KIND[kind])
        return CustomerRequest.objects.create(
            account=account,
            reservation=trip,
            kind=kind,
            topics=", ".join(topics),
            reason=reason,
            message=message,
            task=task,
        )


def pending(account: CustomerAccount, trip: Reservation) -> list[dict]:
    """The account's requests about `trip` whose task is still open, newest first, as
    `{"kind", "date"}` (the day it was sent in the trip's zone, "Nov 20"). One query."""
    rows = (
        account.requests.filter(reservation=trip, task__status__in=Task.UNRESOLVED)
        .order_by("-created_at", "-pk")
        .values_list("kind", "created_at")
    )
    return [{"kind": kind, "date": _short_day(at, trip)} for kind, at in rows]
