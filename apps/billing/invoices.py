"""Customer invoices (APC-106): pure helpers for due dates and the QuickBooks line text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Prefetch, Q

from apps.reservations.models import Reservation, Stop
from apps.reservations.processing import _day, _reference_pk, _short, _with_route

from .models import AccountGroup, BillingAccount, Invoice, Terms

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


# --- selectors -----------------------------------------------------------------------------


@dataclass
class BillingFilters:
    q: str = ""
    date_from: date | None = None
    date_to: date | None = None

    @classmethod
    def from_query(cls, query) -> BillingFilters:
        return cls(
            q=(query.get("q") or "").strip(),
            date_from=_day(query.get("from")),
            date_to=_day(query.get("to")),
        )


@dataclass
class GroupRow:
    group: AccountGroup
    trips: list[Reservation] = field(default_factory=list)
    total: Decimal = Decimal("0")


@dataclass
class AccountRow:
    account: BillingAccount
    groups: list[GroupRow] = field(default_factory=list)
    total: Decimal = Decimal("0")


def ready_queryset(filters: BillingFilters):
    """Trips ready to invoice: processed, on an account, on a booked order, on no invoice.
    A lost order's trips drop out; an invoice already made keeps its lines."""
    qs = (
        Reservation.objects.filter(
            lead__status="booked",
            lead__billing_group__isnull=False,
            review__processed_at__isnull=False,
            invoice_line__isnull=True,
            pickup_date__isnull=False,
        )
        .select_related("lead__contact", "lead__billing_group__account", "vehicle", "review")
        .prefetch_related(Prefetch("stops", queryset=Stop.objects.order_by("sequence")))
        .order_by("pickup_date", "pickup_time", "pk")
    )
    if filters.date_from is not None:
        qs = qs.filter(pickup_date__gte=filters.date_from)
    if filters.date_to is not None:
        qs = qs.filter(pickup_date__lte=filters.date_to)
    if filters.q:
        found = Q(lead__contact__name__icontains=filters.q)
        pk = _reference_pk(filters.q)
        if pk is not None:
            found |= Q(pk=pk)
        qs = qs.filter(found)
    return qs


def ready_accounts(filters: BillingFilters) -> list[AccountRow]:
    """Ready trips grouped account > group in Python after one trip query (accounts and
    groups by name; trips in pickup order)."""
    accounts: dict[int, AccountRow] = {}
    groups: dict[int, GroupRow] = {}
    for trip in _with_route(list(ready_queryset(filters))):
        group = trip.lead.billing_group
        row = groups.get(group.pk)
        if row is None:
            row = groups[group.pk] = GroupRow(group=group)
            account = group.account
            accounts.setdefault(account.pk, AccountRow(account=account)).groups.append(row)
        row.trips.append(trip)
        row.total += trip.line_total
    result = sorted(accounts.values(), key=lambda a: a.account.name.lower())
    for account in result:
        account.groups.sort(key=lambda g: g.group.name.lower())
        account.total = sum((g.total for g in account.groups), Decimal("0"))
    return result


def ready_count() -> int:
    return ready_queryset(BillingFilters()).count()


def group_ready_trips(group: AccountGroup, filters: BillingFilters) -> list[Reservation]:
    return _with_route(list(ready_queryset(filters).filter(lead__billing_group=group)))


def open_invoices(group: AccountGroup) -> list[Invoice]:
    return list(
        Invoice.objects.filter(group=group, status=Invoice.Status.OPEN)
        .prefetch_related("lines__reservation__stops")
        .order_by("-invoice_date", "-pk")
    )
