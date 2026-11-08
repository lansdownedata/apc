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

from . import qbo_sync
from . import terms as terms_module
from .models import (
    AccountGroup,
    BillingAccount,
    Invoice,
    InvoiceLine,
    QboAccountMapping,
    SyncState,
    Terms,
)

# --- Terms ---------------------------------------------------------------------------------

TERM_DAYS = terms_module.TERM_DAYS  # legacy-code days, kept as an alias


def due_date(invoice_date: date, terms: str) -> date:
    return terms_module.due(terms, invoice_date)


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
        .select_related("edited_by")
        .prefetch_related("lines__reservation__stops")
        .order_by("-invoice_date", "-pk")
    )


def voids_waiting(group: AccountGroup) -> list[Invoice]:
    """This group's voided invoices whose QuickBooks void is still to go, or was refused. One
    whose create never reached QuickBooks has nothing there to void."""
    return list(
        Invoice.objects.filter(group=group, status=Invoice.Status.VOID)
        .select_related("replaced_by")
        .exclude(qbo_void_requested_at=None)
        .exclude(qbo_id="")
        .exclude(qbo_sync_state=SyncState.SYNCED)
        .order_by("-voided_at", "-pk")
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
    """Void an invoice: its lines go, so the trips are ready to invoice again. One that is in
    QuickBooks (or whose create is in flight right now, and may land) is queued for the
    QuickBooks void, which the hourly sync or a Retry sends (APC-107). One in this company's
    books inside a closed period is refused; one never sent there may still be voided here
    (APC-109)."""
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("Say why the invoice is voided.")
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status == Invoice.Status.VOID:
            raise InvoiceError("This invoice is already void.")
        if _in_quickbooks(invoice) and closed_in_quickbooks(invoice):
            raise InvoiceError(CLOSED_PERIOD_MESSAGE)
        if invoice.qbo_id or invoice.qbo_push_started_at:
            invoice.qbo_void_requested_at = timezone.now()
            invoice.qbo_sync_state = SyncState.NOT_SYNCED
            invoice.qbo_sync_error = ""
        invoice.lines.all().delete()
        invoice.status = Invoice.Status.VOID
        invoice.voided_at = timezone.now()
        invoice.voided_by = user
        invoice.void_reason = reason[:255]
        invoice.save()
    return invoice


EARLIEST_INVOICE_DATE = date(2020, 1, 1)
CLOSED_PERIOD_MESSAGE = "In a closed period. Correct it with a credit memo or a new invoice."


def _in_quickbooks(invoice: Invoice) -> bool:
    """Is the invoice in the connected company's books? Disconnected (no company to compare),
    one with a QuickBooks id counts as there."""
    if not invoice.qbo_id:
        return False
    realm = qbo_sync.current_realm()
    return not realm or invoice.qbo_realm_id == realm


def _refuse_before_start(invoice_date: date) -> None:
    """An invoice in QuickBooks can't move before the start date: the hourly run would never
    send it again."""
    start = QboAccountMapping.load().customer_docs_from
    if start is not None and invoice_date < start:
        raise InvoiceError(
            "Invoices already in QuickBooks can't be dated before the QuickBooks start "
            f"date ({start:%b %-d, %Y})."
        )


def in_closed_period(doc_date: date | None, config: QboAccountMapping | None = None) -> bool:
    """Is `doc_date` on or before the date the books are closed through in QuickBooks? Pass
    an already loaded `config` to check many dates without a query each."""
    if doc_date is None:
        return False
    closed = (config or QboAccountMapping.load()).book_close_date
    return closed is not None and doc_date <= closed


def closed_in_quickbooks(invoice: Invoice, config: QboAccountMapping | None = None) -> bool:
    """Is the invoice in a closed period by its own date or by the date QuickBooks holds it
    under (an edit that hasn't reached QuickBooks yet)? The earlier one decides."""
    dates = [d for d in (invoice.invoice_date, invoice.qbo_txn_date) if d]
    return in_closed_period(min(dates, default=None), config)


def _edit_note(invoice: Invoice, invoice_date: date, due: date, po: str, memo: str) -> str:
    """The changed parts only, e.g. "date 2026-11-01 → 2026-11-05 · due 2026-12-01 →
    2026-12-05 · PO 4471 → 4480"."""
    parts = []
    if invoice_date != invoice.invoice_date:
        parts.append(f"date {invoice.invoice_date:%Y-%m-%d} → {invoice_date:%Y-%m-%d}")
    if due != invoice.due_date:
        parts.append(f"due {invoice.due_date:%Y-%m-%d} → {due:%Y-%m-%d}")
    if po != invoice.po_number:
        parts.append(f"PO {invoice.po_number or 'none'} → {po or 'none'}")
    if memo != invoice.memo:
        parts.append("memo changed")
    return " · ".join(parts)[:255]


def _validate_edit_fields(invoice_date: date, po_number: str, memo: str) -> tuple[str, str]:
    """The checks an edit and a replacement share: the date inside the window create allows,
    the PO and memo within their columns. Returns the stripped PO and memo. (A blank PO on a
    group that needs one is the caller's to refuse: a replacement leaves it to
    `create_invoice`.)"""
    if not EARLIEST_INVOICE_DATE <= invoice_date <= timezone.localdate() + timedelta(days=365):
        raise InvoiceError("Enter a valid invoice date.")
    po = (po_number or "").strip()
    memo = (memo or "").strip()
    if len(po) > 64:
        raise InvoiceError("The PO number is too long (64 characters at most).")
    if len(memo) > 255:
        raise InvoiceError("The memo is too long (255 characters at most).")
    return po, memo


def edit_invoice(
    invoice: Invoice, *, invoice_date: date, po_number: str, memo: str, user
) -> Invoice:
    """Correct an open invoice's date, PO and memo. Lines and total never change; the due date
    moves only with the date (by the group's terms now). Refused when the invoice or its new
    date is in a closed period. An edit of an invoice in QuickBooks is queued to go there, and
    sent once connected again if made while disconnected (APC-109)."""
    with transaction.atomic():
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("group")
            .get(pk=invoice.pk)
        )
        if invoice.status != Invoice.Status.OPEN:
            raise InvoiceError("Only an open invoice can be edited.")
        po, memo = _validate_edit_fields(invoice_date, po_number, memo)
        if invoice.group.po_required and not po:
            raise InvoiceError("This group needs a PO number.")
        if closed_in_quickbooks(invoice) or in_closed_period(invoice_date):
            raise InvoiceError(CLOSED_PERIOD_MESSAGE)
        linked = _in_quickbooks(invoice)
        if linked:
            _refuse_before_start(invoice_date)
        due = (
            invoice.due_date
            if invoice_date == invoice.invoice_date
            else due_date(invoice_date, invoice.group.effective_terms)
        )
        note = _edit_note(invoice, invoice_date, due, po, memo)
        if not note:
            return invoice
        if linked and invoice.qbo_txn_date is None:
            # Pushed before QuickBooks' date was kept (migration 0011): it holds this one.
            invoice.qbo_txn_date = invoice.invoice_date
        now = timezone.now()
        invoice.invoice_date = invoice_date
        invoice.due_date = due
        invoice.po_number = po
        invoice.memo = memo
        invoice.edited_at = now
        invoice.edited_by = user
        invoice.edit_note = note
        if linked:
            invoice.qbo_edit_requested_at = now
            invoice.qbo_sync_state = SyncState.NOT_SYNCED
            invoice.qbo_sync_error = ""
        invoice.save()
    return invoice


def void_and_replace(
    invoice: Invoice, *, reason: str, invoice_date: date, po_number: str, memo: str, user
) -> Invoice:
    """Void an open invoice and issue a new one from the same trips, all or nothing: if the
    new invoice is refused the old one stays open. The old invoice points at its replacement;
    the void queues the QuickBooks void when the old one is there (APC-109). The PO is the one
    entered: a blank clears it (`create_invoice` then takes the order's, or refuses when the
    group needs one)."""
    with transaction.atomic():
        old = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("group")
            .get(pk=invoice.pk)
        )
        if old.status != Invoice.Status.OPEN:
            raise InvoiceError("Only an open invoice can be voided and replaced.")
        po, memo = _validate_edit_fields(invoice_date, po_number, memo)
        if closed_in_quickbooks(old) or in_closed_period(invoice_date):
            raise InvoiceError(CLOSED_PERIOD_MESSAGE)
        if _in_quickbooks(old):
            _refuse_before_start(invoice_date)
        trip_ids = list(old.lines.values_list("reservation_id", flat=True))
        old = void_invoice(old, reason=reason, user=user)
        new = create_invoice(
            old.group,
            trip_ids,
            invoice_date=invoice_date,
            po_number=po,
            memo=memo or f"Replaces {old.label}",
            user=user,
        )
        old.replaced_by = new
        old.save(update_fields=["replaced_by"])
    return new
