"""What a customer's pages read. `visible_trips` is the one scope every customer page goes
through, so a trip outside it does not exist for the customer (404). Templates get plain
dicts from `trip_row`: a cost, an affiliate or an internal note cannot leak through an
attribute a template reaches for later, because the fields simply aren't there."""

from __future__ import annotations

import re
from decimal import Decimal

from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.functions import Lower, Trim
from django.shortcuts import get_object_or_404

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.leads.models import Lead
from apps.payments import trip_money
from apps.planners.selectors import DISPATCHED_STATUSES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.public.wedding import format_clock
from apps.reservations.models import Passenger, Reservation, Stop

from .models import CustomerAccount

_S = Reservation.TripStatus

# (text, tone). Texts are the spec's table (2026-11-20 §5) word for word.
LABELS: dict[str, tuple[str, str]] = {
    "request_received": ("Request received", "info"),
    "quote_ready": ("Quote ready", "gold"),
    "quote_expired": ("Quote expired", "mute"),
    "confirming": ("Confirming", "gold"),
    "confirmed": ("Confirmed", "ok"),
    "driver_assigned": ("Driver assigned", "ok"),
    "in_progress": ("In progress", "info"),
    "completed": ("Completed", "mute"),
    "cancelled": ("Cancelled", "danger"),
    "no_show": ("No-show", "danger"),
    "closed": ("Closed", "mute"),
}

BUCKETS = ("upcoming", "past", "requests")
_BUCKET_OF = {
    "request_received": "requests",
    "quote_expired": "requests",
    "closed": "requests",
    "completed": "past",
    "cancelled": "past",
    "no_show": "past",
}

# Every cancelled-type status reads "Cancelled"; a no-show has its own label.
_CANCELLED = frozenset(CANCELLED_STATUSES) - {_S.NO_SHOW}
_IN_PROGRESS = frozenset({_S.ON_THE_WAY, _S.ARRIVED, _S.CUSTOMER_IN_CAR})
_BEFORE_BOOKING = {
    Lead.Status.NEW: "request_received",
    Lead.Status.ENGAGED: "confirming",
}


def _wedding_trips() -> QuerySet[Reservation]:
    """`reservations.services.is_wedding_trip` as a filter: a legacy builder leg, or the
    wedding service type (name compared trimmed and case-folded, as the property does)."""
    return Reservation.objects.alias(service=Lower(Trim("service_type__name"))).filter(
        ~Q(source_leg_id="") | Q(service=WEDDING_SERVICE_NAME.lower())
    )


def visible_trips(account: CustomerAccount) -> QuerySet[Reservation]:
    """Trips on leads the account's contact booked or is billed for. Never a wedding order
    (`Lead.is_wedding`: any of its trips is a wedding trip) or a farm-in
    (`Lead.is_farm_in`: the booking contact's company is an affiliate account) — those
    have their own portals. Stops (with airports) and passengers are prefetched so
    `trip_row` costs nothing per row."""
    contact_id = account.contact_id
    return (
        Reservation.objects.filter(
            Q(lead__contact_id=contact_id) | Q(lead__billing_contact_id=contact_id)
        )
        .exclude(Exists(_wedding_trips().filter(lead_id=OuterRef("lead_id"))))
        .exclude(lead__contact__company__is_affiliate_account=True)
        .select_related("lead", "lead__payment", "vehicle")
        .prefetch_related(
            Prefetch(
                "stops", queryset=Stop.objects.select_related("airport").order_by("sequence", "pk")
            ),
            Prefetch("named_passengers", queryset=Passenger.objects.order_by("sort", "pk")),
        )
    )


def trip_or_404(account: CustomerAccount, pk: int) -> Reservation:
    return get_object_or_404(visible_trips(account), pk=pk)


def label_key(trip: Reservation) -> str:
    """Which `LABELS` entry the customer sees (spec §5). A lost order is closed whatever its
    trips say; otherwise a cancelled trip is cancelled at any stage. Before booking the
    order's stage speaks; once booked, the trip's own status does."""
    lead, status = trip.lead, trip.trip_status
    if lead.status == Lead.Status.LOST:
        return "closed"
    if status in _CANCELLED:
        return "cancelled"
    if lead.status == Lead.Status.QUOTED:
        return "quote_expired" if lead.quote_expired else "quote_ready"
    if lead.status in _BEFORE_BOOKING:
        return _BEFORE_BOOKING[lead.status]
    if status == _S.NO_SHOW:
        return "no_show"
    if status in DISPATCHED_STATUSES:
        if status == _S.DONE:
            return "completed"
        return "in_progress" if status in _IN_PROGRESS else "driver_assigned"
    return "confirmed"


def bucket(trip: Reservation) -> str:
    return _BUCKET_OF.get(label_key(trip), "upcoming")


def display_total(trip: Reservation) -> Decimal | None:
    """None while unpriced; for a cancelled trip what the customer paid and we kept;
    otherwise the trip's total."""
    if Decimal(trip.rate or 0) <= 0:
        return None
    if trip.trip_status in _CANCELLED:
        return trip_money.money_for(trip).allocated
    return trip.line_total


_ZIP = re.compile(r"\s+\d{5}(-\d{4})?$")
_COUNTRIES = {"usa", "us", "united states", "united states of america"}


def _place(stop: Stop) -> str:
    """An airport as "Dulles (IAD)", a named place by its name, else the address's town
    ("2200 Clarendon Blvd, Arlington, VA 22201" -> "Arlington, VA")."""
    if stop.airport_id:
        airport = stop.airport
        town = airport.city or airport.name
        return f"{town} ({airport.iata})" if airport.iata else town
    if stop.name.strip():
        return stop.name.strip()
    parts = [p.strip() for p in stop.address.split(",") if p.strip()]
    if parts and parts[-1].lower() in _COUNTRIES:
        parts.pop()
    if len(parts) >= 3:
        return f"{parts[-2]}, {_ZIP.sub('', parts[-1])}"
    return parts[0] if parts else "Location to be confirmed"


def _route(trip: Reservation, stops: list[Stop]) -> str:
    if trip.trip_type == Reservation.TripType.HOURLY:
        hours = trip.billed_hours
        if hours <= 0:
            return "Hourly"
        return f"Hourly, {format(hours.normalize(), 'f')} {'hour' if hours == 1 else 'hours'}"
    if not stops:
        return "Route to be confirmed"
    if len(stops) == 1:
        return _place(stops[0])
    return f"{_place(stops[0])} to {_place(stops[-1])}"


def _passenger(trip: Reservation) -> str:
    """The lead passenger's name, else the first named one's. Never a mobile."""
    named = [p for p in trip.named_passengers.all() if p.name.strip()]
    lead = next((p for p in named if p.is_lead), None)
    return (lead or named[0]).name.strip() if named else ""


def trip_row(trip: Reservation) -> dict:
    """One trip as every customer list shows it. Reads stops and passengers through
    `.all()`, so a trip from `visible_trips` costs no query (never `trip.pickup`, whose
    `.order_by()` ignores the prefetch); any other trip falls back to a query each."""
    stops = sorted(trip.stops.all(), key=lambda s: (s.sequence, s.pk))
    day = trip.pickup_date
    text, tone = LABELS[label_key(trip)]
    return {
        "pk": trip.pk,
        "date": f"{day:%a, %b} {day.day}" if day else "Date to be confirmed",
        "time": format_clock(trip.pickup_time) if trip.pickup_time else "Time to be confirmed",
        # The pickup zone on the trip's own day; blank zone -> the business zone.
        "zone": trip.pickup_tz_label,
        "route": _route(trip, stops),
        "passenger": _passenger(trip),
        "confirmation": trip.la_confirmation or trip.reference,
        "label": text,
        "tone": tone,
        "total": display_total(trip),
        # The customer's own reference arrives with `Reservation.customer_reference`.
        "reference": "",
        "has_receipt": False,
    }
