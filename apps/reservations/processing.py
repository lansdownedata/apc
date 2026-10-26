"""Trip Processing (APC-102, spec 2026-10-25 §4).

A billing agent works each finished trip on its own: confirms the times, settles the
overtime and the price, charges or refunds the customer, and locks it. This module decides
which trips are ready, groups them by customer for the list, and (Task 3) processes,
unlocks and adds a charge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta

from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.dispatch.selectors import coverage_prefetch

from .models import Reservation, Stop, TripReview

S = Reservation.TripStatus
# Statuses processing settles (spec §4.1): the trip ran, or it carries a charge.
SETTLE_STATUSES = (S.DONE, S.LATE_CANCEL, S.NO_SHOW)
# Never processed: nothing ran and nothing is owed.
NEVER = (S.CANCELLED, S.COVID_CANCELLATION)
DEFAULT_DAYS = 30


@dataclass
class Filters:
    view: str = "ready"  # "ready" | "processed"
    q: str = ""
    date_from: date | None = None
    date_to: date | None = None

    @classmethod
    def from_query(cls, query) -> Filters:
        today = timezone.localdate()
        view = query.get("view") or "ready"
        return cls(
            view=view if view in ("ready", "processed") else "ready",
            q=(query.get("q") or "").strip(),
            date_from=_day(query.get("from")) or today - timedelta(days=DEFAULT_DAYS),
            date_to=_day(query.get("to")) or today,
        )


def _day(raw) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


@dataclass
class CustomerGroup:
    contact: object
    trips: list = field(default_factory=list)

    @property
    def oldest(self) -> date | None:
        return self.trips[0].pickup_date if self.trips else None

    @property
    def pays(self) -> str:
        plans = [getattr(t.lead, "payment", None) for t in self.trips]
        card = next((p for p in plans if p is not None and p.stripe_payment_method_id), None)
        return (
            f"Card ••{card.card_last4}"
            if card is not None and card.card_last4
            else ("Card on file" if card is not None else "No card")
        )


def _base(filters: Filters):
    qs = (
        Reservation.objects.filter(
            lead__status="booked",
            pickup_date__range=(filters.date_from, filters.date_to),
        )
        .exclude(trip_status__in=NEVER)
        .select_related("lead__contact", "lead__payment", "vehicle", "review")
        .prefetch_related(
            Prefetch("stops", queryset=Stop.objects.order_by("sequence")),
            coverage_prefetch(),
        )
    )
    if filters.q:
        match = Q(lead__contact__name__icontains=filters.q)
        pk = _reference_pk(filters.q)
        if pk is not None:
            match |= Q(pk=pk)
        qs = qs.filter(match)
    return qs


def _reference_pk(q: str) -> int | None:
    """ "APC-600142" (any case) → 142, the trip's pk (`Reservation.reference`)."""
    from apps.leads.models import QUOTE_PREFIX

    text = q.strip().upper()
    prefix = f"{QUOTE_PREFIX}-"
    if text.startswith(prefix) and text[len(prefix) :].isdigit():
        n = int(text[len(prefix) :]) - 600_000
        return n if n > 0 else None
    return None


def _order(trips) -> list:
    return sorted(trips, key=lambda t: (t.pickup_date or date.max, t.pickup_time or time.max, t.pk))


def ready_trips(filters: Filters) -> list[Reservation]:
    """Finished trips not yet processed (spec §4.1): Done, Late Cancel, No Show; Cancelled
    by Affiliate only when there is money to settle; and a trip with no status whose
    scheduled end + grace has passed ("needs a status" — LA statuses can be missing)."""
    from apps.payments import trip_money
    from apps.tasks import post_trip
    from apps.tasks.models import TaskConfig

    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    now = timezone.now()
    out = []
    unprocessed = _base(filters).filter(
        Q(review__isnull=True) | Q(review__processed_at__isnull=True)
    )
    for trip in unprocessed:
        trip.needs_status = not trip.trip_status
        if trip.trip_status in SETTLE_STATUSES:
            out.append(trip)
        elif trip.trip_status == S.CANCELLED_BY_AFFILIATE:
            money = trip_money.money_for(trip)
            if money.allocated != 0 or money.line_total != 0:
                out.append(trip)
        elif not trip.trip_status and post_trip.has_ended(trip, now=now, grace=grace):
            out.append(trip)
    return _order(out)


def processed_trips(filters: Filters) -> list[Reservation]:
    """Processed trips in the range, the most recently processed first."""
    trips = list(_base(filters).filter(review__processed_at__isnull=False))
    for trip in trips:
        trip.needs_status = False
    return sorted(trips, key=lambda t: t.review.processed_at, reverse=True)


def group_by_customer(trips) -> list[CustomerGroup]:
    """One list, grouped by the order's contact (Moe rejected two side-by-side lists),
    customers sorted by their oldest trip, trips in pickup order within each."""
    groups: dict[int, CustomerGroup] = {}
    for trip in _order(trips):
        contact = trip.lead.contact
        groups.setdefault(contact.pk, CustomerGroup(contact=contact)).trips.append(trip)
    return sorted(groups.values(), key=lambda g: (g.oldest or date.max, g.contact.pk))


def review_of(trip) -> TripReview | None:
    try:
        return trip.review
    except TripReview.DoesNotExist:
        return None


__all__ = [
    "SETTLE_STATUSES",
    "CustomerGroup",
    "Filters",
    "group_by_customer",
    "processed_trips",
    "ready_trips",
    "review_of",
]
