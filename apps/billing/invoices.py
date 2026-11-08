"""Customer invoices (APC-106): pure helpers for due dates and the QuickBooks line text."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from apps.reservations.processing import _short

from .models import Terms

# --- Terms ---------------------------------------------------------------------------------

TERM_DAYS: dict[str, int] = {
    Terms.DUE_ON_RECEIPT: 0,
    Terms.NET_15: 15,
    Terms.NET_30: 30,
    Terms.NET_45: 45,
}


def days(terms: str) -> int:
    """Days from the invoice date to the due date for a payment-terms value."""
    try:
        return TERM_DAYS[terms]
    except KeyError:
        raise ValueError(f"Unknown payment terms {terms!r}") from None


def due_date(invoice_date: date, terms: str) -> date:
    return invoice_date + timedelta(days=days(terms))


# --- Line text -----------------------------------------------------------------------------


def trip_figure(trip) -> Decimal:
    """The trip's price before gratuity: discounted base + overtime + extras."""
    return trip.discounted_base + trip.overtime_amount + trip.extras_total


def _money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _hours(hours: Decimal) -> str:
    """`4` not `4.00`, `2.5` not `2.50`."""
    return f"{Decimal(hours).normalize():f}"


def _kind(trip) -> str:
    """ "Hourly, 4 hrs + 1 hr overtime" or "Transfer + 2 hrs overtime"."""
    hourly = trip.trip_type == "hourly"
    kind = "Hourly" if hourly else "Transfer"
    if hourly:
        billed = Decimal(trip.billed_hours)
        kind += f", {_hours(billed)} {'hr' if billed == 1 else 'hrs'}"
    overtime = Decimal(trip.overtime_hours or 0)
    if overtime > 0:
        kind += f" + {_hours(overtime)} {'hr' if overtime == 1 else 'hrs'} overtime"
    return kind


def line_description(trip) -> str:
    """The two-line invoice description for one trip. `trip.stops` should be prefetched
    (`order_by("sequence")`) so this adds no queries."""
    stops = list(trip.stops.all())
    head = [
        f"Trip #{trip.reference}",
        _kind(trip),
        trip.pickup_date.strftime("%a %b %-d") if trip.pickup_date else "",
        trip.vehicle.name if trip.vehicle else "",
    ]
    figure = f"Trip {_money(trip_figure(trip))}"
    discount = trip.subtotal - trip.discounted_base
    if discount > 0:
        figure += f" (after {_money(discount)} discount)"
    gratuity = f"Gratuity {_money(trip.gratuity)}" if trip.gratuity > 0 else ""
    route = " → ".join(_short(s) for s in (stops[0], stops[-1])) if stops else ""
    tail = [route, figure, gratuity, f"Total {_money(trip.line_total)}"]
    return "\n".join(" · ".join(part for part in line if part) for line in (head, tail))
