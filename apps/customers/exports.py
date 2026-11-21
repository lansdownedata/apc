"""The Trips spreadsheet: a customer's trips as CSV, for expenses."""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from apps.reservations.models import Reservation

from . import receipts, selectors
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


def _payment_method(trip: Reservation) -> str:
    lead = trip.lead
    if lead.billing_group_id is not None:
        return "Account"
    plan = getattr(lead, "payment", None)
    return receipts.card_text(plan.card_brand, plan.card_last4) if plan is not None else ""


def _address(stop) -> str:
    return stop.address.strip() or stop.name.strip()


def trips_csv(
    account: CustomerAccount,
    *,
    f: selectors.TripFilter | None = None,
    trip_ids: list[int] | None = None,
) -> str:
    """The ticked trips (those inside the account's scope) when `trip_ids` is given, else
    every trip the filter shows, unpaged. Paid is read once per order, never per trip."""
    if trip_ids is not None:
        trips = selectors.ticked_trips(account, trip_ids)
    else:
        trips = selectors.filtered_trips(account, f or selectors.TripFilter(tab="all"))
    paid = selectors.paid_amounts(trips)
    rows = selectors.trip_rows(trips, paid)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(HEADER)
    for trip, row in zip(trips, rows, strict=True):
        stops = sorted(trip.stops.all(), key=lambda s: (s.sequence, s.pk))
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
                _safe(_payment_method(trip)),
            ]
        )
    return out.getvalue()
