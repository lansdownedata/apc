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

WINDOWS = ((30, "Last 30 days"), (90, "Last 90 days"), (365, "Last 12 months"))
DEFAULT_WINDOW = 90
# A sub-score under this is called out as a weak spot on the ranking.
WEAK_BELOW = 60


@dataclass(frozen=True)
class Rule:
    """How one metric becomes a 0-100 sub-score: a straight line from `worst` to `best`.
    The wording here is what Settings → Affiliate scoring shows, so the documentation and
    the maths can't drift apart."""

    key: str
    label: str
    weak_label: str
    measures: str
    best: float
    worst: float
    full_marks: str
    zero: str


RULES: tuple[Rule, ...] = (
    Rule(
        "cancellations",
        "Doesn't cancel",
        "Cancellations",
        "Trips the affiliate cancelled, out of the trips they held.",
        0,
        0.2,
        "No cancellations",
        "1 in 5 trips cancelled, or more",
    ),
    Rule(
        "issues",
        "Trips without issues",
        "Issues",
        "Issues logged in trip reviews (vehicle, driver, service, complaint), per trip.",
        0,
        1,
        "No issues",
        "One issue per trip, or more",
    ),
    Rule(
        "confirmation",
        "Confirms quickly",
        "Slow to confirm",
        "The usual (median) time from our offer to their acknowledgement.",
        1,
        24,
        "Within 1 hour",
        "24 hours or longer",
    ),
    Rule(
        "driver_info",
        "Sends driver info early",
        "Late driver info",
        "How long before pickup the driver's name, cell and vehicle usually (median) arrive.",
        24,
        0,
        "24 hours before pickup, or earlier",
        "At pickup time, or later",
    ),
    Rule(
        "rating",
        "Our rating",
        "Low rating",
        "The average of the 1-5 stars we give the affiliate in trip reviews.",
        5,
        1,
        "5 stars",
        "1 star",
    ),
    Rule(
        "pricing",
        "Pricing",
        "Pricing",
        "Each payout against the median payout for the same vehicle type in the window.",
        0.9,
        1.3,
        "10% under the median, or cheaper",
        "30% over the median, or dearer",
    ),
)


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

    def value(self, key: str) -> float | None:
        """The raw number behind one rule, or None when there's nothing to score."""
        return {
            "cancellations": self.cancellation_rate,
            "issues": self.issues_per_trip,
            "confirmation": self.confirm_hours,
            "driver_info": self.driver_info_hours,
            "rating": self.rating,
            "pricing": self.price_ratio,
        }[key]

    def _said(self, key: str) -> str:
        """The raw number in words — what the scorecard shows beside each bar."""
        if key == "cancellations":
            held = self.trips + self.cancellations
            if not held:
                return "No trips yet"
            rate = f" ({self.cancellation_rate:.0%})" if self.cancellations else ""
            return f"{self.cancellations} of {held} cancelled{rate}"
        if key == "issues":
            if not self.trips:
                return "No trips yet"
            if not self.issues:
                return f"No issues on {self.trips} {_plural(self.trips, 'trip')}"
            text = f"{self.issues} {_plural(self.issues, 'issue')} on {self.trips} "
            text += _plural(self.trips, "trip")
            if self.complaints:
                text += f", {self.complaints} {_plural(self.complaints, 'complaint')}"
            return text
        if key == "confirmation":
            if self.confirm_hours is None:
                return "No confirmations recorded"
            return f"{_hours(self.confirm_hours)} to confirm, median"
        if key == "driver_info":
            if self.driver_info_hours is None:
                return "No driver-info times recorded"
            if self.driver_info_hours < 0:
                return f"{_hours(-self.driver_info_hours)} after pickup, median"
            return f"{_hours(self.driver_info_hours)} before pickup, median"
        if key == "rating":
            if self.rating is None:
                return "Not rated yet"
            return (
                f"{self.rating:.1f} average from {self.ratings} {_plural(self.ratings, 'review')}"
            )
        if self.price_ratio is None:
            return "No payouts to compare"
        pct = round((self.price_ratio - 1) * 100)
        if pct == 0:
            return "At the median for its vehicle types"
        return f"{abs(pct)}% {'over' if pct > 0 else 'under'} the median for its vehicle types"

    @property
    def rows(self) -> list[dict]:
        """One line per rule for the scorecard: label, the number in words, the sub-score."""
        return [
            {
                "label": rule.label,
                "said": self._said(rule.key),
                "points": round(self.parts[rule.key]) if rule.key in self.parts else None,
            }
            for rule in RULES
        ]

    @property
    def weak(self) -> list[tuple[str, int]]:
        """The two lowest sub-scores under `WEAK_BELOW` — (label, points), worst first."""
        low = sorted(
            (points, rule.weak_label)
            for rule in RULES
            if (points := self.parts.get(rule.key)) is not None and points < WEAK_BELOW
        )
        return [(label, round(points)) for points, label in low[:2]]

    @property
    def score_display(self) -> int | None:
        return None if self.score is None else round(self.score)


def _plural(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def _hours(hours: float) -> str:
    return f"{hours:.0f} h" if hours >= 1 else f"{hours * 60:.0f} min"


def _sub_scores(card: Scorecard) -> dict[str, float]:
    return {
        rule.key: _scale(value, best=rule.best, worst=rule.worst)
        for rule in RULES
        if (value := card.value(rule.key)) is not None
    }


def describe(weights: dict[str, float] | None = None) -> list[dict]:
    """The scoring rules in words, heaviest first — Settings → Affiliate scoring."""
    weights = weights if weights is not None else settings.AFFILIATE_SCORE_WEIGHTS
    total = sum(weights.get(rule.key, 0) for rule in RULES) or 1
    rows = [
        {
            "label": rule.label,
            "measures": rule.measures,
            "full_marks": rule.full_marks,
            "zero": rule.zero,
            "weight": weights.get(rule.key, 0),
            "share": round(weights.get(rule.key, 0) / total * 100),
        }
        for rule in RULES
    ]
    return sorted(rows, key=lambda r: -r["weight"])


def window(days: int | None, today: date | None = None) -> tuple[int, date, date]:
    """(days, start, end) for a window choice; anything unknown falls back to the default.
    The dates are business-timezone calendar days, compared to trips' local pickup dates."""
    from datetime import timedelta

    from django.utils import timezone

    days = days if days in dict(WINDOWS) else DEFAULT_WINDOW
    end = today or timezone.localdate()
    return days, end - timedelta(days=days), end


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
