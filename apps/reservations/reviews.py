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
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Flight, FlightDirection, Reservation, TripIssue, TripReview, TripStatusEvent

_PICKUP_EVENTS = (Reservation.TripStatus.ARRIVED, Reservation.TripStatus.CUSTOMER_IN_CAR)
_UNSET = object()


class ReviewError(Exception):
    """Something the review can't accept; the message is shown to the user."""


def from_local(trip: Reservation, day: date, clock: time) -> datetime:
    """A date + clock read in the trip's zone, as an aware instant."""
    return datetime.combine(day, clock, tzinfo=ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE))


class OvertimeBasis(StrEnum):
    """Which clock the overtime suggestion reads (client rule, 2026-10-19; APC-91)."""

    HOURLY = "hourly"  # time on the job beyond the billed hours
    TRANSFER_WAIT = "transfer_wait"  # waiting at the pickup, from the scheduled time
    AIRPORT_ARRIVAL = "airport_arrival"  # waiting from the flight's actual arrival


# Arrivals from US territories clear no customs, so they get the domestic grace.
DOMESTIC_COUNTRIES = frozenset({"US", "PR", "VI", "GU", "AS", "MP"})


def _flight_time(flight: Flight | None) -> datetime | None:
    """The best arrival time the flight has: actual, else estimated, else scheduled."""
    return None if flight is None else flight.best_at


def _time_source(flight: Flight) -> str:
    """Which of the flight's times `best_at` is — the copy says so, because a scheduled
    time on a flight never refreshed day-of is a weak stand-in for when it landed."""
    if flight.actual_at:
        return "actual"
    return "estimated" if flight.estimated_at else "scheduled"


def basis_for(trip: Reservation, pickup=_UNSET) -> tuple[OvertimeBasis, Flight | None]:
    """An hourly trip measures time over the billed hours. A transfer measures the wait
    at pickup — from the flight's arrival when the pickup is a verified arriving flight
    with a usable time, otherwise from the scheduled pickup.

    `pickup` is the trip's first stop when the caller already has it (the review queue
    prefetches stops; `trip.pickup` would bypass that with a query per trip)."""
    if trip.trip_type == Reservation.TripType.HOURLY:
        return OvertimeBasis.HOURLY, None
    if pickup is _UNSET:
        pickup = trip.pickup
    if pickup is not None and pickup.airport_id and pickup.flight_id:
        flight = pickup.flight
        if flight.direction == FlightDirection.ARRIVAL and _flight_time(flight):
            return OvertimeBasis.AIRPORT_ARRIVAL, flight
    return OvertimeBasis.TRANSFER_WAIT, None


def _is_international(flight: Flight) -> bool:
    from apps.addresses.models import Airport

    iata = (flight.other_airport_iata or "").upper()
    if not iata:
        return False
    origin = Airport.objects.filter(iata=iata).only("country").first()
    return origin is not None and origin.country not in DOMESTIC_COUNTRIES


def grace_minutes_for(
    trip: Reservation, basis: OvertimeBasis, config, *, international: bool = False
) -> int:
    """The grace for this trip under `basis`, from the TaskConfig rule table.
    `international` only matters for an airport arrival."""
    from apps.leads.models import VehicleType

    if basis is OvertimeBasis.HOURLY:
        vehicle = trip.vehicle
        large = vehicle is not None and vehicle.overtime_class == VehicleType.OvertimeClass.LARGE
        return config.grace_large_minutes if large else config.grace_standard_minutes
    if basis is OvertimeBasis.TRANSFER_WAIT:
        return config.transfer_grace_minutes
    if international:
        return config.airport_international_grace_minutes
    return config.airport_domestic_grace_minutes


def _over_minutes(review: TripReview, basis: OvertimeBasis, flight: Flight | None) -> int | None:
    """Minutes on the basis clock, floored at zero; None until the times it needs exist."""
    trip = review.reservation
    if basis is OvertimeBasis.HOURLY:
        actual = review.actual_minutes
        if actual is None:
            return None
        return max(0, actual - int(Decimal(trip.billed_hours) * 60))
    if review.actual_pickup_at is None:
        return None
    start = _flight_time(flight) if basis is OvertimeBasis.AIRPORT_ARRIVAL else trip.pickup_at
    if start is None:
        return None
    return max(0, int((review.actual_pickup_at - start).total_seconds() // 60))


def suggest_overtime(over_minutes: int | None, *, grace_minutes: int, block_minutes: int) -> int:
    """0 inside the grace; past it, the minutes beyond the grace round up to whole blocks
    (an hour, by default) — each hour started bills, the grace itself never does."""
    if over_minutes is None or over_minutes <= grace_minutes:
        return 0
    block = max(int(block_minutes), 1)
    return math.ceil((over_minutes - grace_minutes) / block) * block


@dataclass(frozen=True)
class _Clock:
    """Everything the suggestion and its explanation need, resolved once per review."""

    basis: OvertimeBasis
    flight: Flight | None
    grace: int
    block: int
    over: int | None
    international: bool
    time_source: str  # "actual" · "estimated" · "scheduled" — which flight time the clock used
    # An airport pickup whose flight isn't verified: the wait rule applies, and we say so.
    fallback: bool
    vehicle_label: str

    @property
    def suggested(self) -> int:
        return suggest_overtime(self.over, grace_minutes=self.grace, block_minutes=self.block)


def _clock(review: TripReview, config, *, pickup=_UNSET) -> _Clock:
    trip = review.reservation
    if pickup is _UNSET and trip.trip_type != Reservation.TripType.HOURLY:
        pickup = trip.pickup  # one query, shared by the basis and the fallback check
    basis, flight = basis_for(trip, pickup)
    fallback = basis is OvertimeBasis.TRANSFER_WAIT and bool(
        pickup is not None and pickup.airport_id and (pickup.flight_id or pickup.flight_number)
    )
    international = flight is not None and _is_international(flight)
    return _Clock(
        basis=basis,
        flight=flight,
        grace=grace_minutes_for(trip, basis, config, international=international),
        block=config.overtime_block_minutes,
        over=_over_minutes(review, basis, flight),
        international=international,
        time_source=_time_source(flight) if flight is not None else "",
        fallback=fallback,
        vehicle_label=trip.vehicle.name.lower() if trip.vehicle is not None else "vehicle",
    )


def _prefilled_times(
    trip: Reservation, basis: OvertimeBasis
) -> tuple[datetime | None, datetime | None]:
    """Pickup and drop-off from the status log. On an hourly trip the clock starts when the
    car is on location (Arrived); on a transfer the wait runs until the passenger boards,
    so Customer In Car is the pickup when it was logged."""
    events = list(
        TripStatusEvent.objects.filter(
            reservation=trip, status__in=(*_PICKUP_EVENTS, Reservation.TripStatus.DONE)
        ).values_list("status", "created_at")
    )
    boarded = [at for status, at in events if status == Reservation.TripStatus.CUSTOMER_IN_CAR]
    pickups = [at for status, at in events if status in _PICKUP_EVENTS]
    if basis is not OvertimeBasis.HOURLY and boarded:
        pickups = boarded
    dones = [at for status, at in events if status == Reservation.TripStatus.DONE]
    return (min(pickups) if pickups else None, max(dones) if dones else None)


def review_for(trip: Reservation) -> TripReview:
    """The trip's review, created on first ask. Blank actual times are filled from the
    status log each time — an event that lands later still makes it in."""
    review, _ = TripReview.objects.get_or_create(reservation=trip)
    if review.is_complete or (review.actual_pickup_at and review.actual_dropoff_at):
        return review
    pickup, dropoff = _prefilled_times(trip, basis_for(trip)[0])
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

    review.suggested_overtime_minutes = _clock(review, TaskConfig.load()).suggested


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
    affiliate_overtime_override=_UNSET,
    affiliate_overtime_note: str = "",
) -> TripReview:
    """Apply what the form sent. Fields left out keep their value.

    Passing `billable_overtime_minutes` or `overtime_waived` is the overtime decision —
    it stamps who made it. Until then billable tracks the suggestion.
    """
    with transaction.atomic():
        return _save_review(
            review,
            user=user,
            actual_pickup_at=actual_pickup_at,
            actual_dropoff_at=actual_dropoff_at,
            billable_overtime_minutes=billable_overtime_minutes,
            overtime_waived=overtime_waived,
            waive_reason=waive_reason,
            affiliate_rating=affiliate_rating,
            affiliate_overtime_override=affiliate_overtime_override,
            affiliate_overtime_note=affiliate_overtime_note,
        )


def _save_review(
    review: TripReview,
    *,
    user,
    actual_pickup_at,
    actual_dropoff_at,
    billable_overtime_minutes,
    overtime_waived,
    waive_reason,
    affiliate_rating,
    affiliate_overtime_override,
    affiliate_overtime_note,
) -> TripReview:
    """`save_review` inside one transaction: the review and the trip's Overtime line are
    two records of one fact and must never be left disagreeing."""
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

    if affiliate_overtime_override is not _UNSET:
        if affiliate_overtime_override is None:
            review.affiliate_overtime_override = None
            review.affiliate_overtime_note = ""
        else:
            amount = Decimal(affiliate_overtime_override)
            if amount < 0:
                raise ReviewError("The affiliate overtime can't be negative.")
            note = (affiliate_overtime_note or "").strip()
            if not note:
                raise ReviewError("Add a note saying why the affiliate overtime is adjusted.")
            review.affiliate_overtime_override = amount.quantize(_CENTS, rounding=ROUND_HALF_UP)
            review.affiliate_overtime_note = note[:255]

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
    if deciding:
        # A decision is money (spec 2026-10-25 §3.2); a suggestion never is.
        from . import pricing

        pricing.apply_review_overtime(review)
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
        _stamp_driver_pay(review)
        review.save(
            update_fields=[
                "completed_at",
                "completed_by",
                "driver_hourly_rate",
                "driver_pay_amount",
                "updated_at",
            ]
        )
        from . import pricing

        pricing.apply_review_overtime(review)
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
    # Half-up here, not in the database: MySQL (dev/test) and Postgres (prod) round a
    # third decimal differently (see `dispatch.views._payout`).
    return (Decimal(minutes) * Decimal(rate) / 60).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _pct_gratuity(trip: Reservation, base: Decimal) -> Decimal:
    """The trip's gratuity percentage on an overtime base — only when the gratuity is a
    percentage line (a flat gratuity adds nothing). Mirrors `Reservation.overtime_gratuity`
    for an amount that is not on the trip yet."""
    pct = Decimal(trip.gratuity_pct or 0)
    if Decimal(trip.gratuity_flat or 0) > 0 or pct <= 0:
        return Decimal("0.00")
    return (base * pct / 100).quantize(_CENTS, rounding=ROUND_HALF_UP)


def affiliate_share(trip: Reservation, coverage) -> tuple[Decimal, str]:
    """The affiliate's share of the trip's base, and how it was found (APC-61).

    "factor" when the payout is exactly what the trip's factor pays; otherwise "payout" —
    a flat `affiliate_cost` or a payout agreed separately — as payout ÷ subtotal. The share
    is of the base only: gratuity pays nobody a share (the `vendor_pay` rule)."""
    payout = Decimal(coverage.payout or 0)
    if trip.vendor_pay_mode == Reservation.VENDOR_PAY_FACTOR and payout == trip.vendor_pay:
        return Decimal(trip.cost_ratio_pct or 0) / 100, "factor"
    subtotal = trip.subtotal
    if subtotal <= 0:
        return Decimal("0"), "payout"
    return payout / subtotal, "payout"


def _stamp_driver_pay(review: TripReview) -> None:
    """Freeze an in-house driver's pay on the review; farmed-out trips have none."""
    from apps.dispatch.models import Assignment

    coverage = (
        Assignment.objects.active()
        .filter(reservation_id=review.reservation_id)
        .select_related("driver")
        .first()
    )
    review.driver_hourly_rate = review.driver_pay_amount = None
    if coverage is None or not coverage.is_in_house:
        return
    rate = Decimal(coverage.driver.hourly_rate or 0)
    if rate > 0 and review.actual_minutes is not None:
        review.driver_hourly_rate = rate
        review.driver_pay_amount = _money(review.actual_minutes, rate)


@dataclass(frozen=True)
class Figures:
    """Everything the two panes show, from one review. Customer billing is a person's
    call; the affiliate's overtime follows what was billed (APC-61), and an in-house
    driver is paid total time on the job at their hourly rate."""

    billed_minutes: int
    actual_minutes: int | None
    over_minutes: int | None
    suggested_minutes: int
    rule: str
    basis: str  # an OvertimeBasis value: which clock `over_minutes` is on
    grace_minutes: int
    # customer billing
    billable_minutes: int
    customer_rate: Decimal
    customer_amount: Decimal  # the overtime base: billable minutes at the discounted rate
    customer_overtime_gratuity: Decimal
    customer_overtime_total: Decimal
    # driver pay: "affiliate" · "in_house" · "none"
    coverage: str
    provider: str
    payout: Decimal
    # affiliate
    affiliate_share: Decimal | None = None
    affiliate_share_basis: str = ""
    affiliate_overtime_derived: Decimal | None = None
    affiliate_overtime_amount: Decimal | None = None
    affiliate_overtime_overridden: bool = False
    expected_affiliate_amount: Decimal | None = None
    # in-house
    driver_id: int | None = None
    driver_hourly_rate: Decimal | None = None
    driver_pay_amount: Decimal | None = None

    @property
    def affiliate_share_pct(self) -> Decimal | None:
        if self.affiliate_share is None:
            return None
        return (self.affiliate_share * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _span(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour bills" if hours == 1 else f"{hours} hours bill"
    return f"{minutes} min bills"


def _rule(clock: _Clock) -> str:
    """One sentence for the review screen: what was measured, the grace, the outcome."""
    over = clock.over
    if over is None:
        text = (
            "Enter the actual times to see the suggestion."
            if clock.basis is OvertimeBasis.HOURLY
            else "Enter the actual pickup time to see the suggestion."
        )
    else:
        if clock.basis is OvertimeBasis.HOURLY:
            what, grace, who = (
                f"{over} min over",
                f"{clock.grace}-min grace",
                f" for a {clock.vehicle_label}",
            )
        elif clock.basis is OvertimeBasis.TRANSFER_WAIT:
            what, grace, who = (
                f"{over} min waiting at pickup",
                f"{clock.grace}-min transfer grace",
                "",
            )
        else:
            kind = "international" if clock.international else "domestic"
            what, grace, who = (
                f"{over} min after the flight's {clock.time_source} arrival",
                f"{clock.grace}-min {kind} grace",
                "",
            )
        if over <= clock.grace:
            text = f"{what}, inside the {grace}, so nothing to suggest."
        else:
            text = f"{what}; {grace}{who}, so {_span(clock.suggested)}."
    if clock.fallback:
        text += " No verified arrival time for the flight, so the transfer wait rule applies."
    return text


def figures(review: TripReview, coverage, *, config=None, pickup=_UNSET) -> Figures:
    """`coverage` is the trip's active Assignment (or None), `config` the TaskConfig and
    `pickup` the trip's first stop — all passed in so a page showing many trips loads
    them once instead of per row."""
    from apps.tasks.models import TaskConfig

    config = config or TaskConfig.load()
    trip = review.reservation
    billed = int(Decimal(trip.billed_hours) * 60)
    actual = review.actual_minutes
    clock = _clock(review, config, pickup=pickup)
    over = clock.over
    billable = 0 if review.overtime_waived else review.billable_overtime_minutes
    rate = trip.discounted_rate  # the discount carries into overtime (client, 2026-10-18)
    if review.decided_at is None:
        # Undecided: the screen shows what the suggestion *would* bill, so the agent can
        # weigh it. It is not on the trip yet — a suggestion is never money (spec §3.2).
        from .pricing import overtime_hours_from_minutes

        customer_amount = (overtime_hours_from_minutes(billable) * rate).quantize(_CENTS)
        ot_gratuity = _pct_gratuity(trip, customer_amount)
    else:
        # Decided: the money is the trip's Overtime line, not a re-derivation from the
        # minutes — one total for the review screen, the order page and the receipt.
        customer_amount = trip.overtime_amount
        ot_gratuity = trip.overtime_gratuity

    kind, provider, payout = "none", "", Decimal("0")
    if coverage is not None:
        kind = "in_house" if coverage.is_in_house else "affiliate"
        provider = coverage.provider_name
        payout = Decimal(coverage.payout or 0)

    driver_pay: dict = {}
    if kind == "affiliate":
        share, basis = affiliate_share(trip, coverage)
        derived = (customer_amount * share).quantize(_CENTS, rounding=ROUND_HALF_UP)
        override = review.affiliate_overtime_override
        amount = derived if override is None else Decimal(override)
        driver_pay = {
            "affiliate_share": share,
            "affiliate_share_basis": basis,
            "affiliate_overtime_derived": derived,
            "affiliate_overtime_amount": amount,
            "affiliate_overtime_overridden": override is not None,
            "expected_affiliate_amount": payout + amount,
        }
    elif kind == "in_house":
        # Stamped at completion. A review completed before the driver had a rate stamped
        # nothing, so it reads live until someone sets one.
        if review.is_complete and review.driver_pay_amount is not None:
            hourly, pay = review.driver_hourly_rate, review.driver_pay_amount
        else:
            hourly = Decimal(coverage.driver.hourly_rate or 0) or None
            pay = _money(actual, hourly) if hourly and actual is not None else None
        driver_pay = {
            "driver_id": coverage.driver_id,
            "driver_hourly_rate": hourly,
            "driver_pay_amount": pay,
        }

    return Figures(
        billed_minutes=billed,
        actual_minutes=actual,
        over_minutes=over,
        suggested_minutes=review.suggested_overtime_minutes,
        rule=_rule(clock),
        basis=str(clock.basis),
        grace_minutes=clock.grace,
        billable_minutes=billable,
        customer_rate=rate,
        customer_amount=customer_amount,
        customer_overtime_gratuity=ot_gratuity,
        customer_overtime_total=customer_amount + ot_gratuity,
        coverage=kind,
        provider=provider,
        payout=payout,
        **driver_pay,
    )
