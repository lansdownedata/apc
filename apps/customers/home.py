"""Home (spec §6.1, mockup 1): the next trip, what is waiting on the customer, and what is
coming up. It reads the trips still ahead and the orders that may owe money; a finished
history is counted once, for the first-visit banner, and never loaded."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import F, Max, OuterRef, Q, Subquery
from django.db.models.query import prefetch_related_objects
from django.utils import timezone

from apps.leads.models import Lead
from apps.payments.models import Charge
from apps.public.wedding import format_clock
from apps.reservations.models import FlightDirection, Reservation, Stop

from . import requests, selectors
from .models import CustomerAccount

# "Your next trip" is one we have confirmed: never a request, a quote or a held deposit.
NEXT_TRIP_KEYS = frozenset({"confirmed", "driver_assigned", "in_progress"})


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
    """ "Meeting flight AA 2114, arriving 8:52 AM PST." for a pickup at an arriving flight.
    The time shows only while the cached flight is still expected, in the airport's own
    zone and always with its abbreviation: never the viewer's. One trip, so one query."""
    stop = Stop.objects.select_related("airport", "airline", "flight").get(pk=first.pk)
    text = f"Meeting flight {stop.flight_label if stop.airline_id else stop.flight_number}"
    at = selectors.expected_flight_at(stop.flight)
    if at:
        zone = ZoneInfo(stop.airport.timezone) if stop.airport.timezone else None
        local = timezone.localtime(at, zone or selectors.trip_zone(trip))
        text += f", arriving {format_clock(local.time())} {local.tzname()}"
    return f"{text}."


def _summary(trip: Reservation) -> str:
    """ "Executive SUV, 2 passengers. Meeting flight AA 2114, arriving 8:52 AM PST." """
    people = f"{trip.passengers} passenger{'' if trip.passengers == 1 else 's'}"
    parts = [f"{trip.vehicle.name}, {people}." if trip.vehicle_id else f"{people.capitalize()}."]
    stops = selectors.stops_of(trip)
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
    """A "Waiting on you" row: the order's soonest trip, what it asks for and where to go
    (`selectors.pay_action`)."""
    url, label = selectors.pay_action(lead, owed)
    if owed is not None:
        return {
            **row,
            "lead": lead.pk,
            "amount": owed[1],
            "note": "Payment due",
            "action_url": url,
            "action_label": label,
        }
    stops = selectors.stops_of(trips[0])
    note = ""
    if len(trips) > 1:
        note = f"{len(trips)} trips on this quote"
    elif trips[0].trip_type == Reservation.TripType.HOURLY and stops:
        note = f"Starts at {selectors.place(stops[0])}"
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
        day = selectors.short_day(trip.lead.created_at, trip)
        return f"Requested {day}. We're preparing your quote."
    if key == "confirming":
        at = held.get(trip.lead_id)
        received = f"Deposit paid {selectors.short_day(at, trip)}. " if at else ""
        return f"{received}We're confirming your vehicle."
    return ""


# How many finished orders that may owe are checked against the scope: room for a few the
# scope then drops (a wedding order, a farm-in) without ever walking a history.
_FINISHED_CHECKED = 4 * selectors.WAITING_PAY_CHECKS


def _finished_may_owe(account: CustomerAccount, ahead: set[int]) -> list[Lead]:
    """Booked orders with no trip ahead (none of `ahead`, the orders Home already holds)
    whose plan says they may still owe: an unpaid deposit, a failed balance, a balance with
    no saved card, an open trip pay link. Found on the plan and its charges, oldest trip
    first, never by loading the history; at most as many as `selectors.owing` will price.

    Two steps so the database cannot do the dear part first: the plan flags pick a few
    orders (usually none: one query), and only those are checked against the scope."""
    first_day = (
        Reservation.objects.filter(lead_id=OuterRef("pk"), pickup_date__isnull=False)
        .order_by("pickup_date")
        .values("pickup_date")[:1]
    )
    maybe = list(
        Lead.objects.filter(selectors.booked_or_billed(account), selectors.may_owe())
        .exclude(pk__in=ahead)
        .annotate(first_day=Subquery(first_day))
        .order_by(F("first_day").asc(nulls_last=True), "pk")
        .values_list("pk", flat=True)[:_FINISHED_CHECKED]
    )
    if not maybe:
        return []
    theirs = selectors.orders_in_scope(account).select_related("payment").in_bulk(maybe)
    return [theirs[pk] for pk in maybe if pk in theirs][: selectors.WAITING_PAY_CHECKS]


def home(account: CustomerAccount) -> dict:
    """Home (spec §6.1). `next_trip`: the soonest confirmed-or-later trip with its heading
    (`selectors.heading`) and `summary` line, or None. `waiting`: one row per order with a
    quote ready (Review and pay, or Review and book on account) or money owed (Pay now),
    soonest first. `upcoming`: every
    upcoming trip but quotes (they wait above), requests included, soonest first.
    `history_banner`: the past-trip count, only before the account's first Home visit (the
    view stamps `history_banner_seen_at` on it). `has_trips`: whether the account has any
    trip at all. The next trip's `can_ask_change`: it takes a change request
    (`requests.can_ask`) and none of the account's is waiting on staff (one query, only
    then).

    Only the trips ahead are read (`selectors.with_labels`): the upcoming bucket and the
    unpriced requests. An order whose trips are all behind it shows only when it may owe
    money (`_finished_may_owe`), and then its trips alone are loaded."""
    labelled = selectors.with_labels(selectors.lean_trips(account))
    ahead = sorted(
        labelled.filter(Q(bucket="upcoming") | Q(label="request_received")),
        key=selectors.soonest,
    )
    keys = {t.pk: selectors.label_key(t) for t in ahead}
    by_lead: dict[int, list[Reservation]] = defaultdict(list)
    for trip in ahead:
        by_lead[trip.lead_id].append(trip)
    quotes = {
        lead_id: group
        for lead_id, trips in by_lead.items()
        if (group := [t for t in trips if keys[t.pk] == "quote_ready"])
    }
    # Soonest trip ahead first (`by_lead` fills in that order), then the finished orders.
    asked = [
        trips[0].lead
        for lead_id, trips in by_lead.items()
        if lead_id in quotes or trips[0].lead.status == Lead.Status.BOOKED
    ]
    finished = _finished_may_owe(account, set(by_lead))
    # `quote_total`, `balance_due_now` and the pay page all read the order's trips.
    prefetch_related_objects([*asked, *finished], "reservations")
    owing = selectors.owing([*asked, *finished])

    behind: dict[int, list[Reservation]] = defaultdict(list)
    settled = [lead for lead in finished if lead.pk in owing]
    if settled:
        theirs = selectors.lean_trips(account).filter(lead__in=settled)
        for trip in sorted(theirs, key=selectors.soonest):
            behind[trip.lead_id].append(trip)

    waiting: list[tuple[Lead, list[Reservation]]] = []
    for lead in asked:
        if lead.pk in owing:
            waiting.append((lead, by_lead[lead.pk]))
        elif lead.pk in quotes:
            waiting.append((lead, quotes[lead.pk]))
    waiting += [(lead, behind[lead.pk]) for lead in settled]
    waiting.sort(key=lambda item: selectors.soonest(item[1][0]))
    upcoming = [t for t in ahead if keys[t.pk] != "quote_ready"]

    shown = {t.pk: t for t in upcoming} | {group[0].pk: group[0] for _, group in waiting}
    selectors.load_details(shown.values())
    rows = dict(zip(shown, selectors.trip_rows(shown.values()), strict=True))
    held = _deposits_held([t for t in upcoming if keys[t.pk] == "confirming"])
    nxt = next((t for t in upcoming if keys[t.pk] in NEXT_TRIP_KEYS), None)
    past = None
    if account.history_banner_seen_at is None:
        past = labelled.filter(bucket="past").count() or None
    return {
        "next_trip": None
        if nxt is None
        else {
            **rows[nxt.pk],
            **selectors.heading(nxt, rows[nxt.pk]),
            "summary": _summary(nxt),
            "can_ask_change": requests.can_ask(nxt, keys[nxt.pk])
            and not any(row["kind"] == "change" for row in requests.pending(account, nxt)),
        },
        "waiting": [
            _waiting_row(rows[group[0].pk], lead, group, owing.get(lead.pk))
            for lead, group in waiting
        ],
        "upcoming": [{**rows[t.pk], "note": _note(t, keys[t.pk], held)} for t in upcoming],
        "history_banner": past,
        "has_trips": bool(shown) or selectors.trips_in_scope(account).exists(),
    }
