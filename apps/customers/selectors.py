"""What a customer's pages read. `visible_trips` is the one scope every customer page goes
through, so a trip outside it does not exist for the customer (404). Templates get plain
dicts from `trip_rows` (a list) or `trip_row` (one trip): a cost, an affiliate or an
internal note cannot leak through an attribute a template reaches for later, because the
fields simply aren't there."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Exists, Max, OuterRef, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import Lower, Trim
from django.db.models.query import prefetch_related_objects
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import dateformat, timezone

from apps.contacts.models import Contact
from apps.core import outbound
from apps.leads.models import QUOTE_PREFIX, Lead
from apps.leads.services import make_deposit_token
from apps.payments import trip_money
from apps.payments.models import Charge, PaymentPlan
from apps.payments.services import TRIP_MONEY_KINDS, pay_state
from apps.planners.selectors import DISPATCHED_STATUSES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.public.wedding import format_clock
from apps.reservations.models import Flight, FlightDirection, Passenger, Reservation, Stop

from . import receipts, services
from .models import CustomerAccount

if TYPE_CHECKING:
    from apps.accounts.models import User

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


_DETAILS = (
    Prefetch("stops", queryset=Stop.objects.select_related("airport").order_by("sequence", "pk")),
    Prefetch("named_passengers", queryset=Passenger.objects.order_by("sort", "pk")),
)


def _scope(account: CustomerAccount) -> QuerySet[Reservation]:
    """`visible_trips` without its stops and passengers: what a page reads to sort, count
    and total a whole history, before `_load_details` on the few rows it shows."""
    contact_id = account.contact_id
    return (
        Reservation.objects.filter(
            Q(lead__contact_id=contact_id) | Q(lead__billing_contact_id=contact_id)
        )
        .exclude(Exists(_wedding_trips().filter(lead_id=OuterRef("lead_id"))))
        .exclude(lead__contact__company__is_affiliate_account=True)
        .select_related("lead", "lead__payment", "vehicle", "review")
    )


def visible_trips(account: CustomerAccount) -> QuerySet[Reservation]:
    """Trips on leads the account's contact booked or is billed for. Never a wedding order
    (`Lead.is_wedding`: any of its trips is a wedding trip) or a farm-in
    (`Lead.is_farm_in`: the booking contact's company is an affiliate account) — those
    have their own portals. Stops (with airports) and passengers are prefetched, and the
    trip review joined, so `trip_row` costs nothing per row."""
    return _scope(account).prefetch_related(*_DETAILS)


def _load_details(trips: Iterable[Reservation]) -> None:
    """Stops and passengers for trips read through `_scope`, in two queries: a page over a
    long history loads them for the rows it shows, not for every trip it counted."""
    prefetch_related_objects(list(trips), *_DETAILS)


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
    if status in receipts.CANCELLED:
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


def paid_amounts(trips: list[Reservation]) -> dict[int, Decimal]:
    """What the customer has paid for each priced trip: the sum
    `trip_money.money_for(trip).allocated` makes, in four queries however many trips and
    orders there are: `trip_money.allocations` for every order at once (three) and one
    charge query for all the trips."""
    priced = [t for t in trips if receipts.priced(t)]
    if not priced:
        return {}
    shares: dict[int, Decimal] = {}
    for order in trip_money.allocations({t.lead_id: t.lead for t in priced}.values()).values():
        shares.update(order)
    charged: defaultdict[int, Decimal] = defaultdict(Decimal)
    refunded: defaultdict[int, Decimal] = defaultdict(Decimal)
    own = (
        Charge.objects.filter(
            reservation_id__in=[t.pk for t in priced],
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
        for t in priced
    }


def _kept(trips: list[Reservation], paid: dict[int, Decimal] | None = None) -> dict[int, Decimal]:
    """What the customer paid and we kept, per priced cancelled trip whose figure is that
    (`receipts.shows_kept`). `paid` is the `paid_amounts` of `trips` when the caller has
    them already."""
    cancelled = [t for t in trips if receipts.priced(t) and receipts.shows_kept(t)]
    if not cancelled:
        return {}
    if paid is None:
        paid = paid_amounts(cancelled)
    return {t.pk: paid[t.pk] for t in cancelled}


def _total(trip: Reservation, kept: dict[int, Decimal]) -> Decimal | None:
    """`receipts.customer_total` with the list's batched kept amounts."""
    return receipts.customer_total(trip, kept.get(trip.pk))


def display_total(trip: Reservation) -> Decimal | None:
    """The customer's figure for one trip (`receipts.customer_total`)."""
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


def trip_rows(trips: Iterable[Reservation], paid: dict[int, Decimal] | None = None) -> list[dict]:
    """The rows for a list of trips. Lists call this, not `trip_row` in a loop: cancelled
    trips' kept amounts are read for the whole list at once. Over `visible_trips` that is
    3 queries, plus 4 when any trip is cancelled, whatever the number of orders."""
    trips = list(trips)
    kept = _kept(trips, paid)
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
        # Raw, for search and export; the label says which kind it is (mockup 2: an
        # imported LimoAnywhere trip reads "Conf. 48213", ours "APC-600412").
        "confirmation": trip.la_confirmation or trip.reference,
        "confirmation_label": f"Conf. {trip.la_confirmation}"
        if trip.la_confirmation
        else trip.reference,
        "label": text,
        "tone": tone,
        "total": _total(trip, kept),
        "reference": trip.customer_reference,
        "has_receipt": receipts.has_receipt(trip, kept.get(trip.pk)),
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

    The kind and amount are the pay page's own answer (`payments.services.pay_state`). That reads
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
        kind, amount = pay_state(lead)
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


def expected_flight_at(flight: Flight | None) -> datetime | None:
    """The flight's estimated (else scheduled) time while it is still expected (scheduled
    or active); None for no flight or one whose time no longer holds. Home's meeting line
    and trip detail's flight line both read this."""
    if flight is None or flight.status not in _FLIGHT_EXPECTED:
        return None
    return flight.estimated_at or flight.scheduled_at


def _meeting(trip: Reservation, first: Stop) -> str:
    """ "Meeting flight AA 2114, arriving 8:52 AM." for a pickup at an arriving flight; the
    time only while the cached flight is still expected. One trip, so one query."""
    stop = Stop.objects.select_related("airport", "airline", "flight").get(pk=first.pk)
    text = f"Meeting flight {stop.flight_label if stop.airline_id else stop.flight_number}"
    at = expected_flight_at(stop.flight)
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


def pay_action(lead: Lead, owed: tuple[str, Decimal] | None) -> tuple[str, str]:
    """(url, button label) for an order waiting on the customer. Both pages are the
    existing token-keyed ones; a quote is reviewed before it is paid, money owed on a
    booked order (`owed`, from `_owing`) goes straight to the pay page."""
    token = make_deposit_token(lead)
    if owed is not None:
        return reverse("quote_pay", args=[token]), "Pay now"
    return reverse("quote_page", args=[token]), "Review and pay"


def _waiting_row(
    row: dict, lead: Lead, trips: list[Reservation], owed: tuple[str, Decimal] | None
) -> dict:
    """A "Waiting on you" row: the order's soonest trip, what it asks for and where to go
    (`pay_action`)."""
    url, label = pay_action(lead, owed)
    if owed is not None:
        return {
            **row,
            "lead": lead.pk,
            "amount": owed[1],
            "note": "Payment due",
            "action_url": url,
            "action_label": label,
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
        "action_url": url,
        "action_label": label,
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
    `history_banner_seen_at` on it). `has_trips`: whether the account has any trip at all.
    The next trip's `can_ask_change`: it takes a change request (`requests.can_ask`) and
    none of the account's is waiting on staff (one query, only then)."""
    from .requests import can_ask, pending

    trips = sorted(_scope(account), key=_soonest)
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
    _load_details(shown.values())
    rows = dict(zip(shown, trip_rows(shown.values()), strict=True))
    held = _deposits_held([t for t in upcoming if keys[t.pk] == "confirming"])
    nxt = next((t for t in upcoming if _is_next(t, keys[t.pk])), None)
    past = sum(1 for key in keys.values() if _bucket_for(key) == "past")
    return {
        "next_trip": None
        if nxt is None
        else {
            **rows[nxt.pk],
            "long_date": _long_date(nxt),
            "summary": _summary(nxt),
            "can_ask_change": can_ask(nxt, keys[nxt.pk])
            and not any(row["kind"] == "change" for row in pending(account, nxt)),
        },
        "waiting": [
            _waiting_row(rows[group[0].pk], lead, group, owing.get(lead.pk))
            for lead, group in waiting
        ],
        "upcoming": [{**rows[t.pk], "note": _note(t, keys[t.pk], held)} for t in upcoming],
        "history_banner": past if past and account.history_banner_seen_at is None else None,
        "has_trips": bool(trips),
    }


# --- Trips ---------------------------------------------------------------------------------

PAGE_SIZE = 50
TABS = (*BUCKETS, "all")
GROUPINGS = ("month", "passenger", "reference")
# "Show more" asks for one more page each time; this keeps a hand-typed `page` sane.
MAX_PAGES = 200
QUERY_MAX = 100
# The rows the foot total sums (spec §7): completed trips and what was kept on cancellations.
TOTALLED_KEYS = frozenset({"completed", "cancelled"})
_NO_GROUP = {"passenger": "No passenger named", "reference": "No reference"}
# `Reservation.reference` is "APC-" + (600000 + pk). Up to 18 digits so the pk fits a
# BIGINT whatever is typed; the prefix is optional ("600412" finds APC-600412 too).
_TRIP_REFERENCE_BASE = 600_000
_TRIP_REFERENCE = re.compile(rf"(?:{QUOTE_PREFIX}\s*-?\s*)?(\d{{1,18}})", re.IGNORECASE)


@dataclass(frozen=True)
class TripFilter:
    """What the Trips page shows. `page` is how many pages of `PAGE_SIZE` rows are shown:
    "Show more" asks for page 2 and gets the first 100 rows back, so a group never splits
    across two fetches."""

    tab: str = "upcoming"
    start: date | None = None
    end: date | None = None
    q: str = ""
    passenger: str = ""
    group: str = "month"
    page: int = 1

    @classmethod
    def from_query(cls, query: QueryDict) -> TripFilter:
        """Read `?tab=&start=&end=&q=&passenger=&group=&page=`. A value it cannot use falls
        back to the default; a range typed backwards is read the right way round."""
        tab = query.get("tab", "")
        group = query.get("group", "")
        start, end = _iso_day(query.get("start", "")), _iso_day(query.get("end", ""))
        if start and end and start > end:
            start, end = end, start
        try:
            page = min(max(int(query.get("page", "1")), 1), MAX_PAGES)
        except ValueError:
            page = 1
        return cls(
            tab=tab if tab in TABS else cls.tab,
            start=start,
            end=end,
            q=query.get("q", "").strip()[:QUERY_MAX],
            passenger=query.get("passenger", "").strip()[:QUERY_MAX],
            group=group if group in GROUPINGS else cls.group,
            page=page,
        )

    @property
    def narrowed(self) -> bool:
        """Whether anything but the tab and grouping limits the trips."""
        return bool(self.start or self.end or self.q or self.passenger)


def _iso_day(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


def _reference_pk(term: str) -> int | None:
    """ "APC-600412", "apc 600412" or "600412" -> 412; anything else -> None."""
    match = _TRIP_REFERENCE.fullmatch(term.strip())
    if match is None:
        return None
    pk = int(match.group(1)) - _TRIP_REFERENCE_BASE
    return pk if pk > 0 else None


def _matching(trips: QuerySet[Reservation], f: TripFilter) -> QuerySet[Reservation]:
    """`trips` narrowed by the date range (the trip's own local pickup date), the passenger
    and the search. Stops and passengers are matched with `Exists`, so a trip with two
    matching stops is still one row."""
    if f.start:
        trips = trips.filter(pickup_date__gte=f.start)
    if f.end:
        trips = trips.filter(pickup_date__lte=f.end)
    if f.passenger:
        rode = Passenger.objects.alias(clean=Trim("name")).filter(
            reservation=OuterRef("pk"), clean__iexact=f.passenger
        )
        trips = trips.filter(Exists(rode))
    if f.q:
        term = f.q
        stops = Stop.objects.filter(
            Q(address__icontains=term) | Q(name__icontains=term), reservation=OuterRef("pk")
        )
        people = Passenger.objects.filter(reservation=OuterRef("pk"), name__icontains=term)
        found = (
            Q(la_confirmation__icontains=term)
            | Q(customer_reference__icontains=term)
            | Q(Exists(stops))
            | Q(Exists(people))
        )
        pk = _reference_pk(term)
        if pk is not None:
            found |= Q(pk=pk)
        trips = trips.filter(found)
    return trips


def _passenger_options(account: CustomerAccount) -> list[tuple[str, str]]:
    """Every named passenger on the account's trips, once each (case aside), A to Z."""
    names = (
        Passenger.objects.filter(reservation__in=visible_trips(account).values("pk"))
        .values_list("name", flat=True)
        .order_by()
        .distinct()
    )
    unique: dict[str, str] = {}
    for name in names:
        name = name.strip()
        if name:
            unique.setdefault(name.casefold(), name)
    return [(name, name) for _, name in sorted(unique.items())]


def _in_tab_order(trips: list[Reservation], tab: str) -> list[Reservation]:
    """Upcoming soonest first; every other tab newest first. Undated trips go last."""
    by_time = sorted(trips, key=_soonest)
    if tab == "upcoming":
        return by_time
    dated = [t for t in by_time if t.pickup_date is not None]
    return dated[::-1] + [t for t in by_time if t.pickup_date is None]


def _group_title(trip: Reservation, group: str) -> str:
    """The trip's group heading; blank for no passenger or no reference."""
    if group == "month":
        day = trip.pickup_date
        return f"{day:%B %Y}" if day else "Date to be confirmed"
    if group == "passenger":
        return _passenger(trip)
    return trip.customer_reference.strip()


def _grouped(
    trips: list[Reservation], rows: list[dict], titles: dict[int, str], group: str
) -> list[dict]:
    """Consecutive rows under one heading each."""
    groups: list[dict] = []
    for trip, row in zip(trips, rows, strict=True):
        title = titles[trip.pk] or _NO_GROUP[group]
        if not groups or groups[-1]["title"].casefold() != title.casefold():
            groups.append({"title": title, "rows": []})
        groups[-1]["rows"].append(row)
    return groups


def _day_text(day: date, year: bool = True) -> str:
    return f"{day:%b} {day.day}, {day.year}" if year else f"{day:%b} {day.day}"


def _range_texts(f: TripFilter) -> tuple[str, str]:
    """(header label, date field text): ("from Oct 1 to Nov 20, 2026", "Oct 1 – Nov 20,
    2026"). The year shows once when both ends share it. Both blank with no range."""
    start, end = f.start, f.end
    if start and end:
        if start == end:
            return f"on {_day_text(start)}", _day_text(start)
        first = _day_text(start, year=start.year != end.year)
        return f"from {first} to {_day_text(end)}", f"{first} – {_day_text(end)}"
    if start:
        return f"from {_day_text(start)}", f"From {_day_text(start)}"
    if end:
        return f"until {_day_text(end)}", f"Until {_day_text(end)}"
    return "", ""


def filtered_trips(account: CustomerAccount, f: TripFilter) -> list[Reservation]:
    """Every trip the filter shows, unpaged, in the tab's order: the Trips page's set."""
    found = [t for t in _matching(visible_trips(account), f) if f.tab in ("all", bucket(t))]
    return _in_tab_order(found, f.tab)


def ticked_trips(account: CustomerAccount, ids: list[int]) -> list[Reservation]:
    """The trips among `ids` the account can see (the rest are dropped), newest first."""
    return _in_tab_order(list(visible_trips(account).filter(pk__in=ids)), "all")


def trips(account: CustomerAccount, f: TripFilter) -> dict:
    """The Trips page (spec §6.2). `groups`: `[{"title", "rows"}]` for the first `f.page`
    pages, in tab order (grouped by passenger or reference: A to Z, the blank group last).
    `count`: trips in the filtered set. `total`: the sum of `display_total` over its
    Completed and Cancelled rows, the whole set and not only the rows shown, with `counted`
    the number of those rows. `passengers`: the passenger filter's options.

    The filtered set is read once without stops or passengers; tab, order and total are
    worked out from it, and only the rows shown load theirs (`_load_details`), so a long
    history costs one lean read however far back it goes. The kept amounts of cancelled
    trips are read once for the whole set (`_kept`: four queries when any trip is
    cancelled), never per row."""
    found = list(_matching(_scope(account), f))
    keys = {t.pk: label_key(t) for t in found}
    has_trips = bool(found) or (f.narrowed and _scope(account).exists())
    if f.tab != "all":
        found = [t for t in found if _bucket_for(keys[t.pk]) == f.tab]

    kept = _kept(found)
    totalled = [t for t in found if keys[t.pk] in TOTALLED_KEYS]
    total = sum(
        (amount for t in totalled if (amount := _total(t, kept)) is not None), Decimal("0.00")
    )

    ordered = _in_tab_order(found, f.tab)
    if f.group == "passenger":
        _load_details(ordered)  # the headings are the passengers' names
    titles = {t.pk: _group_title(t, f.group) for t in ordered}
    if f.group != "month":
        ordered.sort(key=lambda t: (not titles[t.pk], titles[t.pk].casefold()))
    shown = ordered[: PAGE_SIZE * f.page]
    if f.group != "passenger":
        _load_details(shown)
    label, text = _range_texts(f)
    return {
        "groups": _grouped(shown, [_row(t, kept) for t in shown], titles, f.group),
        "count": len(found),
        "total": total,
        "counted": len(totalled),
        "has_more": len(ordered) > len(shown),
        "passengers": _passenger_options(account),
        "range_label": label,
        "range_text": text,
        "has_trips": has_trips,
        "narrowed": f.narrowed,
    }


def quick_ranges() -> list[dict]:
    """The quick date ranges, from today in the business zone: this month, last month,
    this quarter, this year and last year, as ISO dates for the filter."""
    today = timezone.localdate()
    month = today.replace(day=1)
    last = (month - timedelta(days=1)).replace(day=1)
    quarter = month.replace(month=(today.month - 1) // 3 * 3 + 1)

    def span(label: str, start: date, end: date) -> dict:
        return {"label": label, "start": start.isoformat(), "end": end.isoformat()}

    def month_end(first: date) -> date:
        return (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    return [
        span("This month", month, month_end(month)),
        span("Last month", last, month_end(last)),
        span("This quarter", quarter, month_end(quarter.replace(month=quarter.month + 2))),
        span(str(today.year), date(today.year, 1, 1), date(today.year, 12, 31)),
        span(str(today.year - 1), date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    ]


# --- the staff side: the contact profile's access card -------------------------------------

_ACCESS_LABELS = {
    "none": "Not invited",
    "invited": "Invited",
    "active": "Active",
    "revoked": "Revoked",
}


def access_for(contact: Contact) -> dict:
    """The contact profile's Customer portal card (staff side): the state, one line saying
    who did what when, and which actions apply. No query when the contact was loaded with
    `customer_account__invited_by` and `customer_account__revoked_by` selected (the profile
    does); one otherwise.

    `blocked_reason` is why the sending actions can't run now (an affiliate company, which
    only stops an invite; no email on the contact; the go-live switch off). The card shows
    it beside the disabled buttons. Revoke is never blocked."""
    try:
        account = contact.customer_account
    except CustomerAccount.DoesNotExist:
        account = None
    status = account.status if account else "none"
    zone = ZoneInfo(settings.TIME_ZONE)

    def day(moment: datetime) -> str:
        return dateformat.format(moment.astimezone(zone), "M j, Y")

    def by(user: User | None) -> str:
        return f" by {user}" if user else ""

    if account is None:
        first = contact.name.split(" ")[0]
        line = f"{first} can sign in to request trips, see history and download receipts."
    elif status == CustomerAccount.Status.REVOKED:
        line = f"Access revoked {day(account.revoked_at)}{by(account.revoked_by)}."
    elif status == CustomerAccount.Status.INVITED:
        line = (
            f"Invite emailed {day(account.invited_at)}{by(account.invited_by)}."
            if account.invited_at
            else "Invite pending."
        )
    elif account.self_registered or account.invited_at is None:
        line = f"Signed up on their own {day(account.registered_at)}."
    else:
        line = (
            f"Invited {day(account.invited_at)}{by(account.invited_by)}. "
            f"Signed up {day(account.registered_at)}."
        )
    if status == CustomerAccount.Status.ACTIVE and account.last_login:
        seen = account.last_login.astimezone(zone)
        clock = dateformat.format(seen, "M j \\a\\t g:i A")
        line += f" Last signed in {clock} {seen.tzname()}."

    inviting = status in ("none", CustomerAccount.Status.REVOKED)
    if inviting and services.is_affiliate(contact):
        blocked = services.AFFILIATE_REFUSED
    elif not (contact.email or "").strip():
        blocked = services.no_email_message(contact, inviting=inviting)
    elif not outbound.enabled():
        blocked = outbound.OFF_MESSAGE
    else:
        blocked = ""
    return {
        "status": status,
        "label": _ACCESS_LABELS[status],
        "line": line,
        "blocked_reason": blocked,
        "can_invite": inviting and not blocked,
        "show_invite": inviting,
        "show_resend": status == CustomerAccount.Status.INVITED,
        "show_reset": status == CustomerAccount.Status.ACTIVE,
        "can_revoke": status in (CustomerAccount.Status.INVITED, CustomerAccount.Status.ACTIVE),
    }
