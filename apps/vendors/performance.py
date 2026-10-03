"""Affiliate performance scorecard (APC-70).

Per active affiliate, over a window of pickup dates: how fast they confirm, how early they
send driver info, how often they cancel, the issues and ratings from trip reviews, how their
payouts compare, and one weighted reliability score. Five queries, however many affiliates.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from django.conf import settings

from apps.dispatch.models import Assignment
from apps.reservations.models import Reservation, TripIssue, TripReview, TripStatusEvent

from .models import Vendor

LABELS = {
    "confirmation": "Confirms quickly",
    "driver_info": "Sends driver info early",
    "cancellations": "Doesn't cancel",
    "issues": "Trips without issues",
    "rating": "Our rating",
    "pricing": "Pricing",
}


def _scale(value: float, *, best: float, worst: float) -> float:
    """0-100 along a straight line from `worst` (0) to `best` (100), clamped."""
    if best == worst:
        return 100.0
    return max(0.0, min(100.0, (value - worst) / (best - worst) * 100))


@dataclass
class Scorecard:
    vendor: Vendor
    trips: int = 0  # confirmed, farmed-out trips in the window
    cancellations: int = 0
    confirm_hours: float | None = None  # median offer → acknowledgement
    driver_info_hours: float | None = None  # median hours before pickup
    issues: int = 0
    complaints: int = 0
    rating: float | None = None  # mean of TripReview.affiliate_rating
    ratings: int = 0
    price_ratio: float | None = None  # mean payout ÷ the vehicle type's median payout
    enough: bool = False
    score: float | None = None
    parts: dict[str, float] = field(default_factory=dict)  # sub-scores, 0-100

    @property
    def cancellation_rate(self) -> float | None:
        booked = self.trips + self.cancellations
        return self.cancellations / booked if booked else None

    @property
    def issues_per_trip(self) -> float | None:
        return self.issues / self.trips if self.trips else None


def _sub_scores(card: Scorecard) -> dict[str, float]:
    parts: dict[str, float] = {}
    if card.confirm_hours is not None:
        parts["confirmation"] = _scale(card.confirm_hours, best=1, worst=24)
    if card.driver_info_hours is not None:
        parts["driver_info"] = _scale(card.driver_info_hours, best=24, worst=0)
    if card.cancellation_rate is not None:
        parts["cancellations"] = _scale(card.cancellation_rate, best=0, worst=0.2)
    if card.issues_per_trip is not None:
        parts["issues"] = _scale(card.issues_per_trip, best=0, worst=1)
    if card.rating is not None:
        parts["rating"] = _scale(card.rating, best=5, worst=1)
    if card.price_ratio is not None:
        parts["pricing"] = _scale(card.price_ratio, best=0.9, worst=1.3)
    return parts


def composite(parts: dict[str, float], weights: dict[str, float]) -> float | None:
    """The weighted average of the sub-scores we have; a missing one drops out and the
    rest are re-weighted."""
    total = sum(weights.get(key, 0) for key in parts)
    if not total:
        return None
    return sum(parts[key] * weights.get(key, 0) for key in parts) / total


def scorecards(
    start: date,
    end: date,
    *,
    weights: dict[str, float] | None = None,
    min_trips: int | None = None,
) -> list[Scorecard]:
    """One card per active affiliate for pickups from `start` to `end` inclusive, in
    name order. Dates are the trips' own local pickup dates."""
    weights = weights if weights is not None else settings.AFFILIATE_SCORE_WEIGHTS
    min_trips = settings.AFFILIATE_SCORE_MIN_TRIPS if min_trips is None else min_trips
    cards = {v.pk: Scorecard(vendor=v) for v in Vendor.objects.filter(status=Vendor.Status.ACTIVE)}

    rows = list(
        Assignment.objects.filter(
            vendor__isnull=False, reservation__pickup_date__range=(start, end)
        )
        .select_related("reservation")
        .order_by("offered_at", "pk")
    )
    confirm: dict[int, list[float]] = defaultdict(list)
    info: dict[int, list[float]] = defaultdict(list)
    payouts: dict[int, list[tuple[int | None, float]]] = defaultdict(list)
    by_type: dict[int | None, list[float]] = defaultdict(list)
    held_by: dict[int, int] = {}  # trip → the vendor who ran it
    last_holder: dict[int, int] = {}  # trip → the vendor who most recently held it
    for a in rows:
        if a.status != Assignment.Status.DECLINED:
            last_holder[a.reservation_id] = a.vendor_id
        if a.status != Assignment.Status.CONFIRMED or a.vendor_id not in cards:
            continue
        trip = a.reservation
        held_by[trip.pk] = a.vendor_id
        cards[a.vendor_id].trips += 1
        if a.affiliate_confirmed_at is not None:
            hours = (a.affiliate_confirmed_at - a.offered_at).total_seconds() / 3600
            confirm[a.vendor_id].append(hours)
        if a.driver_info_at is not None and trip.pickup_at is not None:
            info[a.vendor_id].append((trip.pickup_at - a.driver_info_at).total_seconds() / 3600)
        payouts[a.vendor_id].append((trip.vehicle_id, float(a.payout)))
        by_type[trip.vehicle_id].append(float(a.payout))

    cancelled = TripStatusEvent.objects.filter(
        status=Reservation.TripStatus.CANCELLED_BY_AFFILIATE,
        reservation__pickup_date__range=(start, end),
    ).values_list("reservation_id", flat=True)
    for trip_id in set(cancelled):
        vendor_id = last_holder.get(trip_id)
        if vendor_id in cards:
            cards[vendor_id].cancellations += 1

    rated: dict[int, list[int]] = defaultdict(list)
    reviews = TripReview.objects.filter(reservation_id__in=held_by, affiliate_rating__isnull=False)
    for trip_id, rating in reviews.values_list("reservation_id", "affiliate_rating"):
        rated[held_by[trip_id]].append(rating)

    issues = TripIssue.objects.filter(review__reservation_id__in=held_by)
    for trip_id, category in issues.values_list("review__reservation_id", "category"):
        card = cards[held_by[trip_id]]
        card.issues += 1
        card.complaints += category == TripIssue.Category.COMPLAINT

    medians = {vehicle: median(values) for vehicle, values in by_type.items()}
    for vendor_id, card in cards.items():
        if confirm[vendor_id]:
            card.confirm_hours = median(confirm[vendor_id])
        if info[vendor_id]:
            card.driver_info_hours = median(info[vendor_id])
        if rated[vendor_id]:
            card.rating = sum(rated[vendor_id]) / len(rated[vendor_id])
            card.ratings = len(rated[vendor_id])
        ratios = [p / medians[t] for t, p in payouts[vendor_id] if medians[t] > 0]
        if ratios:
            card.price_ratio = sum(ratios) / len(ratios)
        card.enough = card.trips >= min_trips
        if card.enough:
            card.parts = _sub_scores(card)
            card.score = composite(card.parts, weights)
    return sorted(cards.values(), key=lambda c: c.vendor.name.lower())


def ranked(cards: list[Scorecard], *, worst_first: bool = True) -> list[Scorecard]:
    """Scored affiliates by score, then the ones without enough trips, by name."""
    scored = sorted(
        (c for c in cards if c.score is not None),
        key=lambda c: (c.score if worst_first else -c.score, c.vendor.name.lower()),
    )
    return scored + [c for c in cards if c.score is None]
