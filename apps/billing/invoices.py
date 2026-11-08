"""Customer invoices (APC-106): pure helpers for due dates and the QuickBooks line text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.reservations.models import Reservation, Stop
from apps.reservations.processing import _day, _reference_pk, _short, _with_route

from .models import AccountGroup, BillingAccount, Invoice, InvoiceLine, Terms

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
    route = (
        f"{_short(stops[0])} → {_short(stops[-1])}"
        if len(stops) > 1
        else _short(stops[0])
        if stops
        else ""
    )
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

    @property
    def terms_label(self) -> str:
        """ "Net 30": the group's own terms, else its account's."""
        return Terms(self.group.effective_terms).label


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


# --- services ------------------------------------------------------------------------------


class InvoiceError(ValueError):
    """A refusal the accountant can read as-is."""


def _lock_ready(group: AccountGroup, trip_ids: set[int]) -> list[Reservation]:
    """Lock the requested trips that are still ready and billed to `group`. `of=("self",)`
    because the ready filter joins nullable tables and Postgres refuses a plain FOR UPDATE
    there (APC-139)."""
    return list(
        ready_queryset(BillingFilters())
        .select_for_update(of=("self",))
        .filter(pk__in=trip_ids, lead__billing_group=group)
    )


def create_invoice(
    group: AccountGroup,
    trip_ids: list[int],
    *,
    invoice_date: date,
    po_number: str,
    memo: str,
    user,
) -> Invoice:
    """Freeze the chosen trips onto one open invoice: each line keeps the trip's text and
    `line_total` as of now, so later price edits never move it. Posts nothing to the ledger."""
    wanted = set(trip_ids)
    if not wanted:
        raise InvoiceError("Pick at least one trip.")
    with transaction.atomic():
        trips = _lock_ready(group, wanted)
        if len(trips) != len(wanted):
            raise InvoiceError(
                "Pick trips that are processed, billed to this group and not yet invoiced."
            )
        trips = _with_route(trips)
        po = (po_number or "").strip()[:64] or (trips[0].lead.po_number or "").strip()[:64]
        if group.po_required and not po:
            raise InvoiceError("This group needs a PO number.")
        invoice = Invoice.objects.create(
            group=group,
            invoice_date=invoice_date,
            due_date=due_date(invoice_date, group.effective_terms),
            po_number=po,
            memo=(memo or "").strip()[:255],
            total=sum((t.line_total for t in trips), Decimal("0")),
            created_by=user,
        )
        for trip in trips:
            try:
                with transaction.atomic():
                    InvoiceLine.objects.create(
                        invoice=invoice,
                        reservation=trip,
                        description=line_description(trip),
                        amount=trip.line_total,
                    )
            except IntegrityError:
                existing = InvoiceLine.objects.select_related("invoice").get(reservation=trip)
                raise InvoiceError(
                    f"Trip #{trip.reference} is already on {existing.invoice.label}"
                ) from None
    return invoice


def void_invoice(invoice: Invoice, *, reason: str, user) -> Invoice:
    """Void an invoice nobody has pushed to QuickBooks: its lines go, so the trips are ready
    to invoice again."""
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("Say why the invoice is voided.")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status == Invoice.Status.VOID:
            raise InvoiceError("This invoice is already void.")
        if invoice.qbo_id:
            raise InvoiceError("This invoice is in QuickBooks. Voiding it there is APC-107.")
        invoice.lines.all().delete()
        invoice.status = Invoice.Status.VOID
        invoice.voided_at = timezone.now()
        invoice.voided_by = user
        invoice.void_reason = reason[:255]
        invoice.save()
    return invoice
