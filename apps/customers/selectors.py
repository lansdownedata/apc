"""What a customer's pages read. `visible_trips` is the one scope every customer page goes
through, so a trip outside it does not exist for the customer (404). Templates get plain
dicts from `trip_rows` (a list) or `trip_row` (one trip): a cost, an affiliate or an
internal note cannot leak through an attribute a template reaches for later, because the
fields simply aren't there."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Exists, Max, OuterRef, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import Lower, Trim
from django.db.models.query import prefetch_related_objects
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from apps.dispatch.selectors import CANCELLED_STATUSES
from apps.leads.models import Lead
from apps.leads.services import make_deposit_token
from apps.leads.views import _pay_state
from apps.payments import trip_money
from apps.payments.models import Charge, PaymentPlan
from apps.payments.services import TRIP_MONEY_KINDS
from apps.planners.selectors import DISPATCHED_STATUSES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.public.wedding import format_clock
from apps.reservations.models import Flight, FlightDirection, Passenger, Reservation, Stop

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
    """Which `LABELS` entry the customer sees (spec §5). A lost order that never booked is
    closed whatever its trips say; otherwise a cancelled trip is cancelled at any stage.
    Before booking the order's stage speaks; once booked, the trip's own status does."""
    lead, status = trip.lead, trip.trip_status
    if lead.status == Lead.Status.LOST:
        # A booked order is lost only by cancelling it (`payments.services._cancel`).
        # Booking freezes `booked_total` on every trip (`leads.services.book_lead`) and a
        # trip added after booking gets 0, so a blank one means the order never booked.
        if trip.booked_total is None:
            return "closed"
        if status == _S.DONE:
            return "completed"
        return "no_show" if status == _S.NO_SHOW else "cancelled"
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


def _bucket_for(key: str) -> str:
    return _BUCKET_OF.get(key, "upcoming")


def bucket(trip: Reservation) -> str:
    return _bucket_for(label_key(trip))


def _priced(trip: Reservation) -> bool:
    return Decimal(trip.rate or 0) > 0


def _kept(trips: list[Reservation]) -> dict[int, Decimal]:
    """What the customer paid and we kept, per priced cancelled trip: the sum
    `trip_money.money_for(trip).allocated` makes, read with one `allocation` per order and
    one charge query for all the trips, never a set of queries per trip."""
    cancelled = [t for t in trips if _priced(t) and t.trip_status in _CANCELLED]
    if not cancelled:
        return {}
    shares: dict[int, Decimal] = {}
    for lead in {t.lead_id: t.lead for t in cancelled}.values():
        shares.update(trip_money.allocation(lead))
    charged: defaultdict[int, Decimal] = defaultdict(Decimal)
    refunded: defaultdict[int, Decimal] = defaultdict(Decimal)
    own = (
        Charge.objects.filter(
            reservation_id__in=[t.pk for t in cancelled],
            status=Charge.Status.SUCCEEDED,
            kind__in=(*TRIP_MONEY_KINDS, Charge.Kind.REFUND),
        )
        .values("reservation_id", "kind")
        .annotate(total=Sum("amount"))
        .order_by()
    )
    for row in own:
        sums = refunded if row["kind"] == Charge.Kind.REFUND else charged
        sums[row["reservation_id"]] += Decimal(row["total"])
    return {
        t.pk: trip_money.TripMoney(
            line_total=t.line_total,
            prepaid=shares.get(t.pk, trip_money.ZERO),
            charged=charged[t.pk],
            refunded=refunded[t.pk],
        ).allocated
        for t in cancelled
    }


def _total(trip: Reservation, kept: dict[int, Decimal]) -> Decimal | None:
    if not _priced(trip):
        return None
    if trip.trip_status in _CANCELLED:
        return kept[trip.pk]
    return trip.line_total


def display_total(trip: Reservation) -> Decimal | None:
    """None while unpriced; for a cancelled trip what the customer paid and we kept;
    otherwise the trip's total."""
    return _total(trip, _kept([trip]))


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


def trip_rows(trips: Iterable[Reservation]) -> list[dict]:
    """The rows for a list of trips. Lists call this, not `trip_row` in a loop: cancelled
    trips' kept amounts are read for the whole list at once. Over `visible_trips` that is
    3 queries, plus 1 when any trip is cancelled and 2 to 4 per order holding one."""
    trips = list(trips)
    kept = _kept(trips)
    return [_row(trip, kept) for trip in trips]


def trip_row(trip: Reservation) -> dict:
    """One trip's row (a detail page). Works on any trip, prefetched or not."""
    return trip_rows([trip])[0]


def _row(trip: Reservation, kept: dict[int, Decimal]) -> dict:
    """Reads stops and passengers through `.all()`, so a trip from `visible_trips` costs no
    query (never `trip.pickup`, whose `.order_by()` ignores the prefetch); any other trip
    falls back to a query each."""
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
        "total": _total(trip, kept),
        # The customer's own reference arrives with `Reservation.customer_reference`.
        "reference": "",
        "has_receipt": False,
    }


# --- Home ----------------------------------------------------------------------------------

# "Your next trip" is one we have confirmed: never a request, a quote or a held deposit.
NEXT_TRIP_KEYS = frozenset({"confirmed", "driver_assigned", "in_progress"})
# At most this many orders that may owe money are priced per Home load (`_owing`), the ones
# with the soonest trip ahead: each costs the pay page's ledger reads.
WAITING_PAY_CHECKS = 5
_NEVER = datetime.max.replace(tzinfo=UTC)
_FLIGHT_EXPECTED = frozenset({Flight.Status.SCHEDULED, Flight.Status.ACTIVE})


def _soonest(trip: Reservation) -> tuple:
    """Sort key: by the pickup instant (each trip in its own zone), undated last."""
    at = trip.pickup_at
    return (at is None, at or _NEVER, trip.pk)


def _on_upcoming(key: str) -> bool:
    """Home's Upcoming list: the upcoming bucket and unpriced requests (spec §5's "Upcoming,
    Requests"), but not a quote ready to pay, which sits in "Waiting on you"."""
    return key == "request_received" or (_bucket_for(key) == "upcoming" and key != "quote_ready")


def _is_next(trip: Reservation, key: str) -> bool:
    """Confirmed or later, and not a day already gone in the trip's own zone (a trip left
    open after its day is not "next"); one under way always is."""
    if key == "in_progress":
        return True
    if key not in NEXT_TRIP_KEYS:
        return False
    zone = ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)
    return trip.pickup_date is None or trip.pickup_date >= timezone.localdate(timezone=zone)


def _stops(trip: Reservation) -> list[Stop]:
    return sorted(trip.stops.all(), key=lambda s: (s.sequence, s.pk))


def _long_date(trip: Reservation) -> str:
    """ "Thursday, December 3" """
    day = trip.pickup_date
    return f"{day:%A, %B} {day.day}" if day else "Date to be confirmed"


def _short_day(at: datetime, trip: Reservation) -> str:
    """`at` as a date in the trip's own zone, e.g. "Nov 19"."""
    day = timezone.localtime(at, ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)).date()
    return f"{day:%b} {day.day}"


def _owing(leads: list[Lead]) -> dict[int, tuple[str, Decimal]]:
    """{lead id: (Charge.Kind, amount)} for booked orders whose money we will not collect
    ourselves: an unpaid deposit, an open trip pay link, or a balance the cron will not take
    (it failed, or it is due with no saved card: `payments.tasks.charge_due_balances`). A
    balance scheduled on a saved card is not waiting on the customer.

    The kind and amount are the pay page's own answer (`leads.views._pay_state`). That reads
    the ledger, about 11 queries an order, so only an order whose plan flags or an open trip
    link say it may owe is asked, and only the first `WAITING_PAY_CHECKS` of those in the
    order given (Home passes soonest trip ahead first); every other order costs nothing."""
    booked = [
        lead
        for lead in leads
        if lead.status == Lead.Status.BOOKED
        and not lead.on_account
        and getattr(lead, "payment", None) is not None
    ]
    if not booked:
        return {}
    linked = set(
        Charge.objects.filter(
            plan__lead__in=booked, kind=Charge.Kind.TRIP, status=Charge.Status.PENDING
        )
        .exclude(stripe_client_secret="")
        .exclude(reservation=None)
        .values_list("plan__lead_id", flat=True)
    )
    candidates: list[tuple[Lead, bool]] = []
    for lead in booked:
        plan = lead.payment
        uncollected = plan.balance_status == PaymentPlan.BalanceStatus.FAILED or (
            plan.balance_due_now
            and not plan.stripe_payment_method_id
            and plan.balance_status != PaymentPlan.BalanceStatus.PAID
        )
        deposit_open = plan.deposit_status != PaymentPlan.DepositStatus.PAID
        if deposit_open or uncollected or lead.pk in linked:
            candidates.append((lead, uncollected))
    owing = {}
    for lead, uncollected in candidates[:WAITING_PAY_CHECKS]:
        kind, amount = _pay_state(lead)
        if kind in (Charge.Kind.DEPOSIT, Charge.Kind.TRIP) or (
            kind == Charge.Kind.BALANCE and uncollected
        ):
            owing[lead.pk] = (kind, amount)
    return owing


def _deposits_held(trips: list[Reservation]) -> dict[int, datetime]:
    """{lead id: when its deposit was authorized}, for the trips reading Confirming."""
    lead_ids = {t.lead_id for t in trips}
    if not lead_ids:
        return {}
    return dict(
        Charge.objects.filter(
            plan__lead_id__in=lead_ids, kind=Charge.Kind.DEPOSIT, authorized_at__isnull=False
        )
        .values("plan__lead_id")
        .annotate(at=Max("authorized_at"))
        .values_list("plan__lead_id", "at")
        .order_by()
    )


def _meeting(trip: Reservation, first: Stop) -> str:
    """ "Meeting flight AA 2114, arriving 8:52 AM." for a pickup at an arriving flight; the
    time only while the cached flight is still expected. One trip, so one query."""
    stop = Stop.objects.select_related("airport", "airline", "flight").get(pk=first.pk)
    text = f"Meeting flight {stop.flight_label if stop.airline_id else stop.flight_number}"
    flight = stop.flight
    at = (
        flight
        and flight.status in _FLIGHT_EXPECTED
        and (flight.estimated_at or flight.scheduled_at)
    )
    if at:
        zone = ZoneInfo(stop.airport.timezone or trip.pickup_timezone or settings.TIME_ZONE)
        text += f", arriving {format_clock(timezone.localtime(at, zone).time())}"
    return f"{text}."


def _summary(trip: Reservation) -> str:
    """ "Executive SUV, 2 passengers. Meeting flight AA 2114, arriving 8:52 AM." """
    people = f"{trip.passengers} passenger{'' if trip.passengers == 1 else 's'}"
    parts = [f"{trip.vehicle.name}, {people}." if trip.vehicle_id else f"{people.capitalize()}."]
    stops = _stops(trip)
    first = stops[0] if stops else None
    if (
        first is not None
        and first.airport_id
        and first.flight_number
        and first.flight_direction != FlightDirection.DEPARTURE
    ):
        parts.append(_meeting(trip, first))
    return " ".join(parts)


def _waiting_row(
    row: dict, lead: Lead, trips: list[Reservation], owed: tuple[str, Decimal] | None
) -> dict:
    """A "Waiting on you" row: the order's soonest trip, what it asks for and where to go.
    Both pages are the existing token-keyed ones; a quote is reviewed before it is paid,
    money owed on a booked order goes straight to the pay page."""
    token = make_deposit_token(lead)
    if owed is not None:
        return {
            **row,
            "lead": lead.pk,
            "amount": owed[1],
            "note": "Payment due",
            "action_url": reverse("quote_pay", args=[token]),
            "action_label": "Pay now",
        }
    stops = _stops(trips[0])
    note = ""
    if len(trips) > 1:
        note = f"{len(trips)} trips on this quote"
    elif trips[0].trip_type == Reservation.TripType.HOURLY and stops:
        note = f"Starts at {_place(stops[0])}"
    return {
        **row,
        "lead": lead.pk,
        "amount": lead.quote_total,
        "note": note,
        "action_url": reverse("quote_page", args=[token]),
        "action_label": "Review and pay",
    }


def _note(trip: Reservation, key: str, held: dict[int, datetime]) -> str:
    """The line under an Upcoming row, while a trip is still on its way to Confirmed."""
    if key == "request_received":
        return f"Requested {_short_day(trip.lead.created_at, trip)}. We're preparing your quote."
    if key == "confirming":
        at = held.get(trip.lead_id)
        received = f"Deposit received {_short_day(at, trip)}. " if at else ""
        return f"{received}We're confirming your vehicle."
    return ""


def home(account: CustomerAccount) -> dict:
    """Home (spec §6.1). `next_trip`: the soonest confirmed-or-later trip with its long date
    and `summary` line, or None. `waiting`: one row per order with a quote ready (Review and
    pay) or money owed (Pay now), soonest first. `upcoming`: every upcoming trip but quotes
    (they wait above), requests included, soonest first. `history_banner`: the past-trip
    count, only before the account's first Home visit (the view stamps
    `history_banner_seen_at` on it). `has_trips`: whether the account has any trip at all."""
    trips = sorted(visible_trips(account), key=_soonest)
    keys = {t.pk: label_key(t) for t in trips}
    by_lead: dict[int, list[Reservation]] = defaultdict(list)
    for trip in trips:
        by_lead[trip.lead_id].append(trip)
    leads = [group[0].lead for group in by_lead.values()]
    quotes = {
        lead.pk: group
        for lead in leads
        if (group := [t for t in by_lead[lead.pk] if keys[t.pk] == "quote_ready"])
    }
    # Each order's trips still ahead (all of them when every one is past), soonest first.
    ahead = {
        pk: [t for t in group if _bucket_for(keys[t.pk]) != "past"] or group
        for pk, group in by_lead.items()
    }
    asked = sorted(
        (lead for lead in leads if lead.pk in quotes or lead.status == Lead.Status.BOOKED),
        key=lambda lead: (
            _bucket_for(keys[ahead[lead.pk][0].pk]) == "past",
            _soonest(ahead[lead.pk][0]),
        ),
    )
    # `quote_total`, `balance_due_now` and the pay page all read the order's trips.
    prefetch_related_objects(asked, "reservations")
    owing = _owing(asked)

    waiting: list[tuple[Lead, list[Reservation]]] = []
    for lead in asked:
        if lead.pk in owing:
            waiting.append((lead, ahead[lead.pk]))
        elif lead.pk in quotes:
            waiting.append((lead, quotes[lead.pk]))
    waiting.sort(key=lambda item: _soonest(item[1][0]))
    upcoming = [t for t in trips if _on_upcoming(keys[t.pk])]

    shown = {t.pk: t for t in upcoming} | {group[0].pk: group[0] for _, group in waiting}
    rows = dict(zip(shown, trip_rows(shown.values()), strict=True))
    held = _deposits_held([t for t in upcoming if keys[t.pk] == "confirming"])
    nxt = next((t for t in upcoming if _is_next(t, keys[t.pk])), None)
    past = sum(1 for key in keys.values() if _bucket_for(key) == "past")
    return {
        "next_trip": None
        if nxt is None
        else {**rows[nxt.pk], "long_date": _long_date(nxt), "summary": _summary(nxt)},
        "waiting": [
            _waiting_row(rows[group[0].pk], lead, group, owing.get(lead.pk))
            for lead, group in waiting
        ],
        "upcoming": [{**rows[t.pk], "note": _note(t, keys[t.pk], held)} for t in upcoming],
        "history_banner": past if past and account.history_banner_seen_at is None else None,
        "has_trips": bool(trips),
    }
