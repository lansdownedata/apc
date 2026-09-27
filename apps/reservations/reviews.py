"""The post-trip Operations review (APC-59) — every TripReview / TripIssue write.

- `review_for(trip)` gets or creates the trip's review, pre-filling actual times from the
  status log (Arrived / Customer In Car → pickup, Done → drop-off) while they're blank.
- `save_review` records what a person entered: actual times, the overtime decision, the
  affiliate rating. The suggestion is recomputed on every save from the actual times.
- `complete_review` stamps the review and closes the trip's `ops_review` task, which
  opens Stage 2 (APC-58).
- `add_issue` / `resolve_issue` — an open issue keeps the trip from being green-lit.

Times arrive as aware datetimes; `from_local` builds one from a date and a clock read in
the trip's own zone, which is how the form enters them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Reservation, TripIssue, TripReview, TripStatusEvent

_PICKUP_EVENTS = (Reservation.TripStatus.ARRIVED, Reservation.TripStatus.CUSTOMER_IN_CAR)
_UNSET = object()


class ReviewError(Exception):
    """Something the review can't accept; the message is shown to the user."""


def from_local(trip: Reservation, day: date, clock: time) -> datetime:
    """A date + clock read in the trip's zone, as an aware instant."""
    return datetime.combine(day, clock, tzinfo=ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE))


def suggest_overtime(
    billed_hours: Decimal,
    actual_pickup_at: datetime | None,
    actual_dropoff_at: datetime | None,
    *,
    increment_minutes: int,
    grace_minutes: int,
) -> int:
    """Minutes over the billed hours, or 0 inside the grace. Past the grace the whole
    overage counts, rounded up to the increment."""
    if not (actual_pickup_at and actual_dropoff_at):
        return 0
    actual = (actual_dropoff_at - actual_pickup_at).total_seconds() / 60
    over = actual - float(billed_hours) * 60
    if over <= grace_minutes:
        return 0
    step = max(int(increment_minutes), 1)
    return math.ceil(round(over, 6) / step) * step


def _prefilled_times(trip: Reservation) -> tuple[datetime | None, datetime | None]:
    events = list(
        TripStatusEvent.objects.filter(
            reservation=trip, status__in=(*_PICKUP_EVENTS, Reservation.TripStatus.DONE)
        ).values_list("status", "created_at")
    )
    pickups = [at for status, at in events if status in _PICKUP_EVENTS]
    dones = [at for status, at in events if status == Reservation.TripStatus.DONE]
    return (min(pickups) if pickups else None, max(dones) if dones else None)


def review_for(trip: Reservation) -> TripReview:
    """The trip's review, created on first ask. Blank actual times are filled from the
    status log each time — an event that lands later still makes it in."""
    review, _ = TripReview.objects.get_or_create(reservation=trip)
    if review.is_complete or (review.actual_pickup_at and review.actual_dropoff_at):
        return review
    pickup, dropoff = _prefilled_times(trip)
    fields = []
    if review.actual_pickup_at is None and pickup is not None:
        review.actual_pickup_at = pickup
        fields.append("actual_pickup_at")
    if review.actual_dropoff_at is None and dropoff is not None:
        review.actual_dropoff_at = dropoff
        fields.append("actual_dropoff_at")
    if fields:
        _stamp_suggestion(review)
        if review.decided_at is None:
            review.billable_overtime_minutes = review.suggested_overtime_minutes
        review.save(
            update_fields=[
                *fields,
                "suggested_overtime_minutes",
                "billable_overtime_minutes",
                "updated_at",
            ]
        )
    return review


def _stamp_suggestion(review: TripReview) -> None:
    from apps.tasks.models import TaskConfig

    config = TaskConfig.load()
    review.suggested_overtime_minutes = suggest_overtime(
        review.reservation.billed_hours,
        review.actual_pickup_at,
        review.actual_dropoff_at,
        increment_minutes=config.overtime_increment_minutes,
        grace_minutes=config.overtime_grace_minutes,
    )


def save_review(
    review: TripReview,
    *,
    user,
    actual_pickup_at=_UNSET,
    actual_dropoff_at=_UNSET,
    billable_overtime_minutes: int | None = None,
    overtime_waived: bool | None = None,
    waive_reason: str = "",
    affiliate_rating=_UNSET,
    driver_overtime_minutes=_UNSET,
    affiliate_rate=_UNSET,
) -> TripReview:
    """Apply what the form sent. Fields left out keep their value.

    Passing `billable_overtime_minutes` or `overtime_waived` is the overtime decision —
    it stamps who made it. Until then billable tracks the suggestion.
    """
    trip = review.reservation
    if actual_pickup_at is not _UNSET:
        review.actual_pickup_at = actual_pickup_at
    if actual_dropoff_at is not _UNSET:
        review.actual_dropoff_at = actual_dropoff_at
    if (
        review.actual_pickup_at
        and review.actual_dropoff_at
        and review.actual_dropoff_at <= review.actual_pickup_at
    ):
        raise ReviewError("The drop-off has to be after the pickup.")

    if affiliate_rating is not _UNSET:
        if affiliate_rating is not None:
            from apps.dispatch.models import Assignment

            # Active, not just confirmed — the same rule as the post-trip payable kinds.
            coverage = Assignment.objects.active().filter(reservation=trip).first()
            if coverage is None or coverage.is_in_house:
                raise ReviewError("Only a trip an affiliate covered can rate the affiliate.")
            if not 1 <= int(affiliate_rating) <= 5:
                raise ReviewError("Rate the affiliate from 1 to 5.")
        review.affiliate_rating = affiliate_rating

    if driver_overtime_minutes is not _UNSET:
        if driver_overtime_minutes is not None and int(driver_overtime_minutes) < 0:
            raise ReviewError("Driver overtime can't be negative.")
        review.driver_overtime_minutes = driver_overtime_minutes
    if affiliate_rate is not _UNSET:
        if affiliate_rate is not None and Decimal(affiliate_rate) < 0:
            raise ReviewError("The affiliate rate can't be negative.")
        review.affiliate_rate = affiliate_rate

    _stamp_suggestion(review)
    deciding = billable_overtime_minutes is not None or overtime_waived is not None
    if deciding:
        waived = bool(overtime_waived)
        if waived and not (waive_reason or "").strip():
            raise ReviewError("Give a reason for waiving the overtime.")
        if billable_overtime_minutes is not None and int(billable_overtime_minutes) < 0:
            raise ReviewError("Billable overtime can't be negative.")
        review.overtime_waived = waived
        review.waive_reason = waive_reason.strip() if waived else ""
        review.billable_overtime_minutes = 0 if waived else int(billable_overtime_minutes or 0)
        review.decided_by = user
        review.decided_at = timezone.now()
    elif review.decided_at is None:
        review.billable_overtime_minutes = review.suggested_overtime_minutes
    review.save()
    return review


def complete_review(review: TripReview, *, user) -> TripReview:
    """Sign the review off and close the trip's `ops_review` task."""
    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    with transaction.atomic():
        review = (
            TripReview.objects.select_for_update().select_related("reservation").get(pk=review.pk)
        )
        if review.is_complete:
            raise ReviewError("This review is already complete.")
        if not (review.actual_pickup_at and review.actual_dropoff_at):
            raise ReviewError("Enter the actual pickup and drop-off times first.")
        if review.decided_at is None:
            raise ReviewError("Decide the overtime first: approve, change or waive it.")
        review.completed_at = timezone.now()
        review.completed_by = user
        review.save(update_fields=["completed_at", "completed_by", "updated_at"])
    task = Task.objects.filter(
        reservation_id=review.reservation_id, kind="ops_review", status__in=Task.UNRESOLVED
    ).first()
    if task is not None:
        tasks.complete(task, user)
    else:
        # No open task to close (e.g. reviewed before the trip entered post-trip) — still
        # let the overtime decision reach Stage 2.
        tasks.sync(review.reservation.lead)
    return review


def add_issue(review: TripReview, *, category: str, severity: str, note: str, user) -> TripIssue:
    if category not in TripIssue.Category.values:
        raise ReviewError("Pick an issue category.")
    if severity not in TripIssue.Severity.values:
        raise ReviewError("Pick a severity.")
    if not (note or "").strip():
        raise ReviewError("Say what happened.")
    return TripIssue.objects.create(
        review=review, category=category, severity=severity, note=note.strip(), created_by=user
    )


def resolve_issue(issue: TripIssue, *, user) -> TripIssue:
    if issue.is_open:
        issue.resolved_at = timezone.now()
        issue.resolved_by = user
        issue.save(update_fields=["resolved_at", "resolved_by", "updated_at"])
    return issue


# --- the review screen's figures ---------------------------------------------------------

_CENTS = Decimal("0.01")


def _money(minutes: int, rate: Decimal) -> Decimal:
    return (Decimal(minutes) * Decimal(rate) / 60).quantize(_CENTS)


@dataclass(frozen=True)
class Figures:
    """Everything the two panes show, from one review. Customer billing and driver pay
    share the actual times and nothing else."""

    billed_minutes: int
    actual_minutes: int | None
    over_minutes: int | None
    suggested_minutes: int
    rule: str
    # customer billing
    billable_minutes: int
    customer_rate: Decimal
    customer_amount: Decimal
    # driver pay: "affiliate" · "in_house" · "none"
    coverage: str
    provider: str
    payout: Decimal
    driver_minutes: int
    affiliate_rate: Decimal | None
    affiliate_overtime_amount: Decimal | None
    expected_affiliate_amount: Decimal | None


def _rule(over: int | None, *, grace: int, increment: int) -> str:
    if over is None:
        return "Enter the actual times to see the suggestion."
    if over <= grace:
        return f"{over} min over, inside the {grace}-min grace, so nothing to suggest."
    return (
        f"{over} min over. Past the {grace}-min grace, so the whole overage rounds up to "
        f"{increment}-min steps."
    )


def figures(review: TripReview, coverage, *, config=None) -> Figures:
    """`coverage` is the trip's active Assignment (or None), and `config` the TaskConfig —
    both passed in so a page showing many trips loads them once."""
    from apps.tasks.models import TaskConfig

    config = config or TaskConfig.load()
    trip = review.reservation
    billed = int(Decimal(trip.billed_hours) * 60)
    actual = review.actual_minutes
    over = max(0, actual - billed) if actual is not None else None
    billable = 0 if review.overtime_waived else review.billable_overtime_minutes
    rate = Decimal(trip.rate or 0)

    kind, provider, payout = "none", "", Decimal("0")
    if coverage is not None:
        kind = "in_house" if coverage.is_in_house else "affiliate"
        provider = coverage.provider_name
        payout = Decimal(coverage.payout or 0)
    driver = review.driver_overtime_minutes
    if driver is None:
        driver = over or 0

    aff_rate = aff_ot = expected = None
    if kind == "affiliate":
        aff_rate = review.affiliate_rate
        if aff_rate is None and billed:
            aff_rate = (payout / (Decimal(billed) / 60)).quantize(_CENTS)
        aff_ot = _money(driver, aff_rate or 0)
        expected = payout + aff_ot

    return Figures(
        billed_minutes=billed,
        actual_minutes=actual,
        over_minutes=over,
        suggested_minutes=review.suggested_overtime_minutes,
        rule=_rule(
            over,
            grace=config.overtime_grace_minutes,
            increment=config.overtime_increment_minutes,
        ),
        billable_minutes=billable,
        customer_rate=rate,
        customer_amount=_money(billable, rate),
        coverage=kind,
        provider=provider,
        payout=payout,
        driver_minutes=driver,
        affiliate_rate=aff_rate,
        affiliate_overtime_amount=aff_ot,
        expected_affiliate_amount=expected,
    )
