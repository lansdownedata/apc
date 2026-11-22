"""The Trips spreadsheet: a customer's trips as CSV, for expenses."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from decimal import Decimal

from apps.payments.models import Charge
from apps.reservations.models import Reservation, Stop

from . import receipts, selectors, trip_list
from .models import CustomerAccount

HEADER = (
    "Date",
    "Time",
    "Time zone",
    "Confirmation",
    "Status",
    "Passenger",
    "Pickup",
    "Drop-off",
    "Vehicle",
    "Your reference",
    "Total",
    "Paid",
    "Payment method",
)

# A spreadsheet reads a cell starting with one of these as a formula.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe(text: str) -> str:
    """Text a customer (or an address book) typed, made inert for Excel and Sheets."""
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def _money(amount: Decimal | None) -> str:
    return "" if amount is None else f"{amount:.2f}"


def _cards_that_paid(trips: list[Reservation]) -> dict[int, set[tuple[str, str]]]:
    """{order: the (brand, last four) kept on each of its payments}, in one query. A
    payment with no card kept on it is ("", "")."""
    paid: defaultdict[int, set[tuple[str, str]]] = defaultdict(set)
    rows = (
        Charge.objects.filter(
            plan__lead_id__in={t.lead_id for t in trips}, status=Charge.Status.SUCCEEDED
        )
        .exclude(kind=Charge.Kind.REFUND)
        .values_list("plan__lead_id", "card_brand", "card_last4")
        .distinct()
        .order_by()
    )
    for lead_id, brand, last4 in rows:
        paid[lead_id].add((brand, last4) if last4 else ("", ""))
    return paid


def _payment_method(trip: Reservation, cards: dict[int, set[tuple[str, str]]]) -> str:
    """ "Account" for an order billed to one; blank while nothing is paid; else the card
    that paid, named only when every payment on the order was by that one card (the
    receipt's rule: the card on file is the next charge's, and may never have paid)."""
    if trip.lead.billing_group_id is not None:
        return "Account"
    paid_by = cards.get(trip.lead_id)
    if not paid_by:
        return ""
    if len(paid_by) == 1:
        return receipts.card_text(*next(iter(paid_by))) or "Card"
    return "Card"


def _address(stop: Stop) -> str:
    return stop.address.strip() or stop.name.strip()


def trips_csv(
    account: CustomerAccount,
    *,
    f: trip_list.TripFilter | None = None,
    trip_ids: list[int] | None = None,
) -> str:
    """The ticked trips (those inside the account's scope) when `trip_ids` is given, else
    every trip the filter shows (`trip_list.filtered`, the Trips page's own set), unpaged.
    Paid is read once for all of them, never per trip or per order."""
    if trip_ids is not None:
        trips = trip_list.ticked_trips(account, trip_ids)
    else:
        trips = trip_list.export_trips(account, f or trip_list.TripFilter(tab="all"))
    paid = selectors.paid_amounts(trips)
    rows = selectors.trip_rows(trips, paid)
    cards = _cards_that_paid(trips)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for trip, row in zip(trips, rows, strict=True):
        stops = selectors.stops_of(trip)
        pickup = _address(stops[0]) if stops else ""
        dropoff = _address(stops[-1]) if len(stops) > 1 else ""
        text = [
            trip.pickup_date.isoformat() if trip.pickup_date else "",
            row["time"] if trip.pickup_time else "",
            row["zone"],
            row["confirmation"],
            row["label"],
            row["passenger"],
            pickup,
            dropoff,
            trip.vehicle.name if trip.vehicle else "",
            row["reference"],
        ]
        writer.writerow(
            [
                *(_safe(cell) for cell in text),
                _money(row["total"]),
                _money(paid.get(trip.pk)),
                _safe(_payment_method(trip, cards)),
            ]
        )
    return out.getvalue()
