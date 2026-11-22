"""What every customer page reads from: the one scope, the labels, the money and the row.

`visible_trips` is the one scope every customer page goes through, so a trip outside it does
not exist for the customer (404). Templates get plain dicts from `trip_rows`: a cost, an
affiliate or an internal note cannot leak through an attribute a template reaches for later,
because the fields simply aren't there.

The pages built on this live beside it: `home` (Home), `trip_list` (Trips and the
spreadsheet's set), `trip_detail` (one trip) and `staff_access` (the staff card). They
import this module; it imports none of them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings
from django.db.models import (
    Case,
    CharField,
    Exists,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Lower, Trim
from django.db.models.query import prefetch_related_objects
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import Lead
from apps.leads.services import make_deposit_token
from apps.payments import trip_money
from apps.payments.models import Charge, PaymentPlan
from apps.payments.services import TRIP_MONEY_KINDS, pay_state
from apps.planners.selectors import DISPATCHED_STATUSES
from apps.public.services import WEDDING_SERVICE_NAME
from apps.public.wedding import format_clock
from apps.reservations.models import Flight, Passenger, Reservation, Stop

from . import receipts
from .models import CustomerAccount

_S = Reservation.TripStatus

# --- scope ---------------------------------------------------------------------------------


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


def _in_scope(account: CustomerAccount, lead: str) -> Q:
    """The scope, as a filter on `Lead` (`lead=""`) or on a trip through its order
    (`lead="lead__"`): orders the account's contact booked or is billed for. Never a
    wedding order (`Lead.is_wedding`: any of its trips is a wedding trip) or a farm-in
    (`Lead.is_farm_in`: the booking contact's company is an affiliate account) — those
    have their own portals."""
    contact_id = account.contact_id
    order_pk = OuterRef("lead_id" if lead else "pk")
    return (
        (Q(**{f"{lead}contact_id": contact_id}) | Q(**{f"{lead}billing_contact_id": contact_id}))
        & ~Exists(_wedding_trips().filter(lead_id=order_pk))
        & ~Q(**{f"{lead}contact__company__is_affiliate_account": True})
    )


def trips_in_scope(account: CustomerAccount) -> QuerySet[Reservation]:
    """The scope and nothing else (`_in_scope`), for counting, `exists()` and subqueries."""
    return Reservation.objects.filter(_in_scope(account, "lead__"))


def orders_in_scope(account: CustomerAccount) -> QuerySet[Lead]:
    """The orders whose trips are `trips_in_scope`, for a question about the order itself
    (does it owe?) that should not walk its trips."""
    return Lead.objects.filter(_in_scope(account, ""))


def lean_trips(account: CustomerAccount) -> QuerySet[Reservation]:
    """The scope with what a row's label and money read (the order, its plan, the vehicle,
    the trip review) but no stops or passengers: what a page reads to pick, sort and total
    trips, before `load_details` on the few it shows."""
    return trips_in_scope(account).select_related("lead", "lead__payment", "vehicle", "review")


def visible_trips(account: CustomerAccount) -> QuerySet[Reservation]:
    """`lean_trips` with stops (and their airports) and passengers prefetched, so
    `trip_rows` costs nothing per row."""
    return lean_trips(account).prefetch_related(*_DETAILS)


def load_details(trips: Iterable[Reservation]) -> None:
    """Stops and passengers for trips read through `lean_trips`, in two queries: a page over
    a long history loads them for the rows it shows, not for every trip it counted."""
    prefetch_related_objects(list(trips), *_DETAILS)


def trip_or_404(account: CustomerAccount, pk: int) -> Reservation:
    return get_object_or_404(visible_trips(account), pk=pk)


# --- labels and buckets --------------------------------------------------------------------

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
# A booked trip nobody closed stops being "upcoming" once its pickup day is over in its own
# zone: it keeps its label (we do not know how it ended) and moves to Past. One under way
# stays where it is, whatever the date.
PAST_ONCE_DAY_GONE = frozenset({"confirmed", "driver_assigned"})

_IN_PROGRESS = frozenset({_S.ON_THE_WAY, _S.ARRIVED, _S.CUSTOMER_IN_CAR})
_BEFORE_BOOKING = {
    Lead.Status.NEW: "request_received",
    Lead.Status.ENGAGED: "confirming",
}


def label_key(trip: Reservation) -> str:
    """Which `LABELS` entry the customer sees (spec §5). A lost order that never booked is
    closed whatever its trips say; otherwise a cancelled trip is cancelled at any stage.
    Before booking the order's stage speaks; once booked, the trip's own status does.
    `_label_case` is this, clause for clause, for the database."""
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
    if status == _S.DONE:
        return "completed"
    if status in _IN_PROGRESS:
        return "in_progress"
    return "driver_assigned" if status in DISPATCHED_STATUSES else "confirmed"


def _label_case() -> Case:
    """`label_key` for the database, in the same order: the first clause that holds wins."""
    lost = Q(lead__status=Lead.Status.LOST)
    quoted = Q(lead__status=Lead.Status.QUOTED)
    # `Lead.quote_expired` for a Quoted order; a blank expiry never matches.
    expired = Q(lead__quote_expires_at__lte=timezone.now())
    return Case(
        When(lost & Q(booked_total__isnull=True), then=Value("closed")),
        When(lost & Q(trip_status=_S.DONE), then=Value("completed")),
        When(lost & Q(trip_status=_S.NO_SHOW), then=Value("no_show")),
        When(lost, then=Value("cancelled")),
        When(trip_status__in=receipts.CANCELLED, then=Value("cancelled")),
        When(quoted & expired, then=Value("quote_expired")),
        When(quoted, then=Value("quote_ready")),
        *(When(lead__status=status, then=Value(key)) for status, key in _BEFORE_BOOKING.items()),
        When(trip_status=_S.NO_SHOW, then=Value("no_show")),
        When(trip_status=_S.DONE, then=Value("completed")),
        When(trip_status__in=_IN_PROGRESS, then=Value("in_progress")),
        When(trip_status__in=DISPATCHED_STATUSES, then=Value("driver_assigned")),
        default=Value("confirmed"),
        output_field=CharField(),
    )


def trip_zone(trip: Reservation) -> ZoneInfo:
    """The trip's own zone: its pickup's, or the business zone while that is blank."""
    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def day_gone(trip: Reservation) -> bool:
    """Whether the trip's pickup day is over in its own zone. An undated trip's never is."""
    day = trip.pickup_date
    return day is not None and day < timezone.localdate(timezone=trip_zone(trip))


@lru_cache(maxsize=4)
def _today_by_zone(quarter_hour: int) -> dict[date, tuple[str, ...]]:
    """{today's date: the zones where it is that date} at one instant, for every zone a trip
    can carry. Every UTC offset in use is a multiple of 15 minutes, so the answer holds for
    a whole quarter hour and is kept for it (keyed on the instant, so never stale)."""
    now = datetime.fromtimestamp(quarter_hour * 900, tz=UTC)
    by_day: defaultdict[date, list[str]] = defaultdict(list)
    for name in sorted(available_timezones()):
        by_day[now.astimezone(ZoneInfo(name)).date()].append(name)
    return {day: tuple(names) for day, names in by_day.items()}


def _day_gone_q() -> Q:
    """`day_gone` for the database. A date before today everywhere on earth is gone; where
    the date is already later than that, the zones are listed. A blank zone is the
    business zone, as in `trip_zone`."""
    now = timezone.now()
    by_day = _today_by_zone(int(now.timestamp()) // 900)
    earliest = min(by_day)
    gone = Q(pickup_date__lt=earliest)
    for day, zones in by_day.items():
        if day > earliest:
            gone |= Q(pickup_date__lt=day, pickup_timezone__in=zones)
    home = timezone.localdate(now, ZoneInfo(settings.TIME_ZONE))
    if home > earliest:
        gone |= Q(pickup_date__lt=home, pickup_timezone="")
    return gone


def bucket(trip: Reservation, key: str | None = None) -> str:
    """Which of `BUCKETS` the trip sits in: its label's, except that a booked trip left open
    after its day (`PAST_ONCE_DAY_GONE`) is past. `key` is its `label_key` when the caller
    has it."""
    key = label_key(trip) if key is None else key
    if key in PAST_ONCE_DAY_GONE and day_gone(trip):
        return "past"
    return _BUCKET_OF.get(key, "upcoming")


def with_labels(trips: QuerySet[Reservation]) -> QuerySet[Reservation]:
    """`trips` with `label` (`label_key`) and `bucket` (`bucket`) to filter on, so a page
    asks the database for the trips it shows instead of reading a whole history to sort it
    in Python. Aliases, not columns: a row's label is still `label_key(trip)`.
    `test_label_and_bucket_filters_agree_with_python` pins the two together."""
    by_bucket: defaultdict[str, list[str]] = defaultdict(list)
    for key, name in _BUCKET_OF.items():
        by_bucket[name].append(key)
    return trips.alias(label=_label_case()).alias(
        bucket=Case(
            When(label__in=by_bucket["past"], then=Value("past")),
            When(Q(label__in=PAST_ONCE_DAY_GONE) & _day_gone_q(), then=Value("past")),
            When(label__in=by_bucket["requests"], then=Value("requests")),
            default=Value("upcoming"),
            output_field=CharField(),
        )
    )


# --- money ---------------------------------------------------------------------------------


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


def kept_amounts(
    trips: Iterable[Reservation], paid: dict[int, Decimal] | None = None
) -> dict[int, Decimal]:
    """What the customer paid and we kept, per priced cancelled trip whose figure is that
    (`receipts.shows_kept`): four queries when there is one, none otherwise. `paid` is the
    `paid_amounts` of `trips` when the caller has them already."""
    cancelled = [t for t in trips if receipts.priced(t) and receipts.shows_kept(t)]
    if not cancelled:
        return {}
    if paid is None:
        paid = paid_amounts(cancelled)
    return {t.pk: paid[t.pk] for t in cancelled}


def total_for(trip: Reservation, kept: dict[int, Decimal]) -> Decimal | None:
    """`receipts.customer_total` with a list's batched `kept_amounts`."""
    return receipts.customer_total(trip, kept.get(trip.pk))


# --- dates ---------------------------------------------------------------------------------

_NEVER = datetime.max.replace(tzinfo=UTC)


def soonest(trip: Reservation) -> tuple:
    """Sort key: by the pickup instant (each trip in its own zone), undated last."""
    at = trip.pickup_at
    return (at is None, at or _NEVER, trip.pk)


def long_date(trip: Reservation) -> str:
    """ "Thursday, December 3" """
    day = trip.pickup_date
    return f"{day:%A, %B} {day.day}" if day else "Date to be confirmed"


def short_day(at: datetime, trip: Reservation) -> str:
    """`at` as a date in the trip's own zone, e.g. "Nov 19"."""
    day = timezone.localtime(at, trip_zone(trip)).date()
    return f"{day:%b} {day.day}"


def heading(trip: Reservation, row: dict) -> dict:
    """A trip's page heading, from its `trip_rows` row: `heading` "Thursday, December 3 at
    9:10 AM" with `heading_zone` beside it, or, while the day or the time is open, the part
    we know and `when_note` for the rest."""
    day = long_date(trip)
    if trip.pickup_date and trip.pickup_time:
        return {"heading": f"{day} at {row['time']}", "heading_zone": row["zone"], "when_note": ""}
    note = ""
    if trip.pickup_date:
        note = row["time"]
    elif trip.pickup_time:
        note = f"{row['time']} {row['zone']}"
    return {"heading": day, "heading_zone": "", "when_note": note}


_FLIGHT_EXPECTED = frozenset({Flight.Status.SCHEDULED, Flight.Status.ACTIVE})


def expected_flight_at(flight: Flight | None) -> datetime | None:
    """The flight's estimated (else scheduled) time while it is still expected (scheduled
    or active); None for no flight or one whose time no longer holds. Home's meeting line
    and trip detail's flight line both read this."""
    if flight is None or flight.status not in _FLIGHT_EXPECTED:
        return None
    return flight.estimated_at or flight.scheduled_at


# --- the row -------------------------------------------------------------------------------

_ZIP = re.compile(r"\s+\d{5}(-\d{4})?$")
_COUNTRIES = {"usa", "us", "united states", "united states of america"}


def place(stop: Stop) -> str:
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


def stops_of(trip: Reservation) -> list[Stop]:
    """The trip's stops in order, through `.all()`: no query for a trip whose details are
    loaded (never `trip.pickup`, whose `.order_by()` ignores the prefetch)."""
    return sorted(trip.stops.all(), key=lambda s: (s.sequence, s.pk))


def _route(trip: Reservation, stops: list[Stop]) -> str:
    if trip.trip_type == Reservation.TripType.HOURLY:
        hours = trip.billed_hours
        if hours <= 0:
            return "Hourly"
        return f"Hourly, {format(hours.normalize(), 'f')} {'hour' if hours == 1 else 'hours'}"
    if not stops:
        return "Route to be confirmed"
    if len(stops) == 1:
        return place(stops[0])
    return f"{place(stops[0])} to {place(stops[-1])}"


def lead_passenger(trip: Reservation) -> str:
    """The lead passenger's name, else the first named one's. Never a mobile."""
    named = [p for p in trip.named_passengers.all() if p.name.strip()]
    lead = next((p for p in named if p.is_lead), None)
    return (lead or named[0]).name.strip() if named else ""


def trip_rows(
    trips: Iterable[Reservation],
    paid: dict[int, Decimal] | None = None,
    *,
    kept: dict[int, Decimal] | None = None,
) -> list[dict]:
    """The rows for a list of trips (or one: `trip_rows([trip])[0]`), prefetched or not.
    Cancelled trips' kept amounts are read for the whole list at once (`kept_amounts`, from
    `paid` when the caller has the trips' `paid_amounts`), unless the caller hands them in
    as `kept`. Over `visible_trips` that is 3 queries, plus 4 when any trip is cancelled,
    whatever the number of orders."""
    trips = list(trips)
    if kept is None:
        kept = kept_amounts(trips, paid)
    return [_row(trip, kept) for trip in trips]


def _row(trip: Reservation, kept: dict[int, Decimal]) -> dict:
    """A trip whose details are loaded costs no query; any other falls back to one each
    for its stops and passengers."""
    stops = stops_of(trip)
    day = trip.pickup_date
    text, tone = LABELS[label_key(trip)]
    return {
        "pk": trip.pk,
        "date": f"{day:%a, %b} {day.day}" if day else "Date to be confirmed",
        "time": format_clock(trip.pickup_time) if trip.pickup_time else "Time to be confirmed",
        # The pickup zone on the trip's own day; blank zone -> the business zone.
        "zone": trip.pickup_tz_label,
        "route": _route(trip, stops),
        "passenger": lead_passenger(trip),
        # Raw, for search and export; the label says which kind it is (mockup 2: an
        # imported LimoAnywhere trip reads "Conf. 48213", ours "APC-600412").
        "confirmation": trip.la_confirmation or trip.reference,
        "confirmation_label": f"Conf. {trip.la_confirmation}"
        if trip.la_confirmation
        else trip.reference,
        "label": text,
        "tone": tone,
        "total": total_for(trip, kept),
        "reference": trip.customer_reference,
        "has_receipt": receipts.has_receipt(trip, kept.get(trip.pk)),
    }


# --- money an order owes -------------------------------------------------------------------

# At most this many orders that may owe money are priced per page load (`owing`): each
# costs the pay page's ledger reads.
WAITING_PAY_CHECKS = 5


def _open_trip_links() -> QuerySet[Charge]:
    """Pay links sent for one trip and not yet paid (`payments.services.open_trip_link`)."""
    return (
        Charge.objects.filter(kind=Charge.Kind.TRIP, status=Charge.Status.PENDING)
        .exclude(stripe_client_secret="")
        .exclude(reservation=None)
    )


def may_owe() -> Q:
    """A filter on `Lead` for the orders `owing` would look at: booked, paid by card, with
    a plan whose flags (or an open trip pay link) say money may be waiting on the customer.
    A little wider than `owing`'s own check: a card-less balance passes before it is due."""
    return Q(status=Lead.Status.BOOKED, billing_group__isnull=True, payment__isnull=False) & (
        ~Q(payment__deposit_status=PaymentPlan.DepositStatus.PAID)
        | Q(payment__balance_status=PaymentPlan.BalanceStatus.FAILED)
        | (
            Q(payment__stripe_payment_method_id="")
            & ~Q(payment__balance_status=PaymentPlan.BalanceStatus.PAID)
        )
        | Exists(_open_trip_links().filter(plan__lead_id=OuterRef("pk")))
    )


def owing(leads: list[Lead]) -> dict[int, tuple[str, Decimal]]:
    """{lead id: (Charge.Kind, amount)} for booked orders whose money we will not collect
    ourselves: an unpaid deposit, an open trip pay link, or a balance the cron will not take
    (it failed, or it is due with no saved card: `payments.tasks.charge_due_balances`). A
    balance scheduled on a saved card is not waiting on the customer.

    The kind and amount are the pay page's own answer (`payments.services.pay_state`). That
    reads the ledger, about 11 queries an order, so only an order whose plan flags or an
    open trip link say it may owe is asked, and only the first `WAITING_PAY_CHECKS` of those
    in the order given (Home passes soonest trip ahead first); every other order costs
    nothing. Prefetch each lead's `reservations`: the plan and the pay page read them."""
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
        _open_trip_links().filter(plan__lead__in=booked).values_list("plan__lead_id", flat=True)
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
    owed = {}
    for lead, uncollected in candidates[:WAITING_PAY_CHECKS]:
        kind, amount = pay_state(lead)
        if kind in (Charge.Kind.DEPOSIT, Charge.Kind.TRIP) or (
            kind == Charge.Kind.BALANCE and uncollected
        ):
            owed[lead.pk] = (kind, amount)
    return owed


def pay_action(lead: Lead, owed: tuple[str, Decimal] | None) -> tuple[str, str]:
    """(url, button label) for an order waiting on the customer. Both pages are the
    existing token-keyed ones; a quote is reviewed before it is paid, money owed on a
    booked order (`owed`, from `owing`) goes straight to the pay page."""
    token = make_deposit_token(lead)
    if owed is not None:
        return reverse("quote_pay", args=[token]), "Pay now"
    return reverse("quote_page", args=[token]), "Review and pay"
