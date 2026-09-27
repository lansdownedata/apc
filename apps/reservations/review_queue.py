"""The Trip Review list and order view (APC-59, design signed off 2026-09-26).

An order is *in review* once it has entered post-trip (APC-58: every trip finished, so
its trips carry `ops_review` tasks). Its stage:

- **Needs ops review** — a live trip whose review isn't done (a completed TripReview, or
  its `ops_review` task closed by hand on the checklist).
- **In billing** — every trip reviewed, but money is still owed or Accounting's post-trip
  tasks (overtime invoiced, affiliate payable approved / paid) are still open.
- **Closed** — reviewed and settled. Off the default list; reachable by the Stage filter.

Money, per the design: Collected = cash on the ledger; Total due = order total + the
customer overtime approved on completed reviews; Remaining = total due − collected.

Everything comes from one pass of prefetches — the page costs the same for 3 orders or 30.
Route ends come from the prefetched stops, never `Reservation.pickup` (which re-queries).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Exists, OuterRef, Prefetch, Sum
from django.utils import timezone

from apps.core.choices import Account
from apps.dispatch.selectors import coverage_prefetch
from apps.leads.models import Lead
from apps.payments.models import JournalLine
from apps.tasks import post_trip
from apps.tasks.models import Task, TaskConfig, format_local

from . import reviews
from .models import Stop, TripIssue

ZERO = Decimal("0.00")
ACCOUNTING_KINDS = ("overtime_invoiced", "affiliate_payable_approved", "affiliate_paid")
WINDOWS = {"7": 7, "30": 30, "90": 90, "all": None}


class Stage:
    NEEDS_REVIEW = "needs_review"
    BILLING = "billing"
    CLOSED = "closed"
    ORDER = {NEEDS_REVIEW: 0, BILLING: 1, CLOSED: 2}
    LABELS = {NEEDS_REVIEW: "Needs ops review", BILLING: "In billing", CLOSED: "Closed"}


@dataclass(frozen=True)
class Filters:
    stage: str = ""  # "" = open (needs review + billing) · needs_review · billing · closed
    balance: str = ""  # "" · owed · settled
    finished: str = "30"  # days, or "all"
    issues: str = ""  # "1" = only orders with an open issue

    @classmethod
    def from_query(cls, params) -> Filters:
        stage = params.get("stage", "")
        balance = params.get("balance", "")
        finished = params.get("finished", "30")
        return cls(
            stage=stage if stage in Stage.ORDER else "",
            balance=balance if balance in ("owed", "settled") else "",
            finished=finished if finished in WINDOWS else "30",
            issues="1" if params.get("issues") == "1" else "",
        )


@dataclass
class TripRow:
    trip: object
    review: object | None
    coverage: object | None
    state: str  # needs_review · reviewed · cancelled
    ended_by_schedule: bool
    open_issues: int
    pickup_stop: object | None
    dropoff_stop: object | None
    figures: reviews.Figures | None = None

    @property
    def pickup_display(self) -> str:
        at = self.trip.pickup_at
        return format_local(at) if at else ""


@dataclass
class OrderRow:
    lead: Lead
    trips: list[TripRow] = field(default_factory=list)
    finished_at: datetime | None = None
    collected: Decimal = ZERO
    order_total: Decimal = ZERO
    approved_overtime: Decimal = ZERO
    approved_trips: int = 0
    awaiting_trips: int = 0
    accounting_open: bool = False

    @property
    def live_trips(self) -> list[TripRow]:
        return [t for t in self.trips if t.state != "cancelled"]

    @property
    def reviewed(self) -> int:
        return sum(1 for t in self.live_trips if t.state == "reviewed")

    @property
    def to_review(self) -> int:
        return sum(1 for t in self.live_trips if t.state == "needs_review")

    @property
    def open_issues(self) -> int:
        return sum(t.open_issues for t in self.trips)

    @property
    def total_due(self) -> Decimal:
        return self.order_total + self.approved_overtime

    @property
    def remaining(self) -> Decimal:
        return max(self.total_due - self.collected, ZERO)

    @property
    def order_balance(self) -> Decimal:
        """What the booked order itself still owes, before any overtime."""
        return max(self.order_total - self.collected, ZERO)

    @property
    def stage(self) -> str:
        if self.to_review:
            return Stage.NEEDS_REVIEW
        if self.remaining > ZERO or self.accounting_open:
            return Stage.BILLING
        return Stage.CLOSED

    @property
    def stage_label(self) -> str:
        return Stage.LABELS[self.stage]

    @property
    def finished_display(self) -> str:
        return format_local(self.finished_at)

    @property
    def service_summary(self) -> str:
        names = []
        for t in self.live_trips:
            label = t.trip.service_label
            if label not in names:
                names.append(label)
        return names[0] if len(names) == 1 else f"{len(self.live_trips)} trips"


def _in_review(window_days: int | None):
    entered = Task.objects.filter(lead=OuterRef("pk"), kind="ops_review")
    if window_days is not None:
        entered = entered.filter(created_at__gte=timezone.now() - timedelta(days=window_days))
    return Lead.objects.filter(status=Lead.Status.BOOKED).filter(Exists(entered))


def _load(leads_qs) -> list[OrderRow]:
    leads = list(
        leads_qs.select_related("contact", "payment").prefetch_related(
            "reservations__service_type",
            "reservations__vehicle",
            Prefetch("reservations__stops", queryset=Stop.objects.order_by("sequence")),
            "reservations__review",
            Prefetch(
                "reservations__review__issues",
                queryset=TripIssue.objects.open(),
                to_attr="open_issue_rows",
            ),
            coverage_prefetch("reservations__assignments"),
            Prefetch(
                "tasks",
                queryset=Task.objects.filter(kind__in=(*ACCOUNTING_KINDS, "ops_review")),
                to_attr="review_tasks",
            ),
        )
    )
    if not leads:
        return []
    cash = (
        JournalLine.objects.filter(entry__lead__in=leads, account=Account.CASH)
        .values("entry__lead_id")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    )
    collected = {r["entry__lead_id"]: (r["d"] or ZERO) - (r["c"] or ZERO) for r in cash}
    config = TaskConfig.load()
    grace = timedelta(hours=config.post_trip_grace_hours)
    now = timezone.now()
    return [_order_row(lead, collected.get(lead.pk, ZERO), config, grace, now) for lead in leads]


def _order_row(lead, collected, config, grace, now) -> OrderRow:
    tasks = defaultdict(list)
    for t in lead.review_tasks:
        tasks[(t.reservation_id, t.kind)].append(t)
    plan = getattr(lead, "payment", None)
    trips = sorted(lead.reservations.all(), key=lambda r: (r.pickup_date, r.pickup_time, r.pk))
    row = OrderRow(
        lead=lead,
        collected=collected,
        order_total=Decimal(plan.quote_total if plan is not None else lead.quote_total),
        finished_at=post_trip.order_finished_at(trips, now=now, grace=grace),
        accounting_open=any(
            t.status in Task.UNRESOLVED for t in lead.review_tasks if t.kind in ACCOUNTING_KINDS
        ),
    )
    for trip in trips:
        review = getattr(trip, "review", None)
        coverage = trip.active_list[0] if trip.active_list else None
        stops = list(trip.stops.all())
        ops = tasks.get((trip.pk, "ops_review"), [])
        closed_by_hand = bool(ops) and all(t.is_closed for t in ops)
        if trip.is_cancelled:
            state = "cancelled"
        elif (review is not None and review.is_complete) or closed_by_hand:
            state = "reviewed"
        else:
            state = "needs_review"
        figures = reviews.figures(review, coverage, config=config) if review else None
        if state == "needs_review":
            row.awaiting_trips += 1
        elif review is not None and review.is_complete and figures.customer_amount > ZERO:
            row.approved_overtime += figures.customer_amount
            row.approved_trips += 1
        row.trips.append(
            TripRow(
                trip=trip,
                review=review,
                coverage=coverage,
                state=state,
                ended_by_schedule=not trip.trip_status and state != "cancelled",
                open_issues=len(getattr(review, "open_issue_rows", [])) if review else 0,
                pickup_stop=stops[0] if stops else None,
                dropoff_stop=stops[-1] if len(stops) > 1 else None,
                figures=figures,
            )
        )
    if row.finished_at is None:
        entered = [t.created_at for t in lead.review_tasks if t.kind == "ops_review"]
        row.finished_at = min(entered) if entered else None
    return row


def _matches(row: OrderRow, filters: Filters) -> bool:
    if filters.stage:
        if row.stage != filters.stage:
            return False
    elif row.stage == Stage.CLOSED:
        return False
    if filters.balance == "owed" and row.remaining <= ZERO:
        return False
    if filters.balance == "settled" and row.remaining > ZERO:
        return False
    return not (filters.issues and not row.open_issues)


def _sort_key(row: OrderRow):
    finished = row.finished_at or timezone.now()
    return (Stage.ORDER[row.stage], finished, row.lead.pk)


def orders_in_review(filters: Filters) -> list[OrderRow]:
    rows = _load(_in_review(WINDOWS[filters.finished]))
    return sorted((r for r in rows if _matches(r, filters)), key=_sort_key)


@dataclass(frozen=True)
class Counts:
    in_review: int
    needs_review: int
    billing: int
    with_issues: int
    owed: Decimal


def counts(filters: Filters, rows: list[OrderRow] | None = None) -> Counts:
    """The count strip: open orders in the finished window, whatever else is filtered."""
    if rows is None:
        rows = _load(_in_review(WINDOWS[filters.finished]))
    open_rows = [r for r in rows if r.stage != Stage.CLOSED]
    return Counts(
        in_review=len(open_rows),
        needs_review=sum(1 for r in open_rows if r.stage == Stage.NEEDS_REVIEW),
        billing=sum(1 for r in open_rows if r.stage == Stage.BILLING),
        with_issues=sum(1 for r in open_rows if r.open_issues),
        owed=sum((r.remaining for r in open_rows), ZERO),
    )


def list_page(filters: Filters) -> tuple[list[OrderRow], Counts]:
    """The list view's rows and count strip from one load."""
    rows = _load(_in_review(WINDOWS[filters.finished]))
    listed = sorted((r for r in rows if _matches(r, filters)), key=_sort_key)
    return listed, counts(filters, rows)


def order_review(lead_id: int) -> OrderRow | None:
    """One order's review page, or None when it isn't in review."""
    rows = _load(_in_review(None).filter(pk=lead_id))
    return rows[0] if rows else None
