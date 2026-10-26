"""Money per trip (APC-101, spec 2026-10-25 §4.4).

Charges still hang off the order's `PaymentPlan`, but each finished trip is settled on its
own: what the order *prepaid* (deposit, balance, a historical final bill) is allocated to
its trips in pickup order, each taking up to the total frozen when the order booked
(`Reservation.booked_total`), and the trip's own TRIP charges and refunds sit on top. The
difference between the trip's live `line_total` and that allocation is what processing
charges (> 0) or refunds (< 0).

The allocation is deterministic whatever order trips are processed in: it reads frozen
booked totals, never the processing sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from django.db.models import Sum

from .models import Charge

ZERO = Decimal("0.00")
_CENTS = Decimal("0.01")

# Order-level money: collected on the order as a whole, belonging to no trip.
PREPAID_KINDS = (Charge.Kind.DEPOSIT, Charge.Kind.BALANCE, Charge.Kind.FINAL)


@dataclass(frozen=True)
class TripMoney:
    line_total: Decimal
    prepaid: Decimal
    charged: Decimal
    refunded: Decimal

    @property
    def allocated(self) -> Decimal:
        """What the customer has already paid for this trip."""
        return (self.prepaid + self.charged - self.refunded).quantize(_CENTS)

    @property
    def difference(self) -> Decimal:
        """> 0: to charge now. < 0: to refund. 0: nothing owed."""
        return (self.line_total - self.allocated).quantize(_CENTS)


def _sum(qs) -> Decimal:
    return Decimal(qs.aggregate(s=Sum("amount"))["s"] or 0).quantize(_CENTS)


def order_prepaid(lead) -> Decimal:
    """Cash collected on the order that belongs to no trip: succeeded deposit, balance and
    historical final-bill charges, less refunds made at order level (no trip)."""
    plan = getattr(lead, "payment", None)
    if plan is None:
        return ZERO
    succeeded = plan.charges.filter(status=Charge.Status.SUCCEEDED)
    collected = _sum(succeeded.filter(kind__in=PREPAID_KINDS))
    refunded = _sum(succeeded.filter(kind=Charge.Kind.REFUND, reservation__isnull=True))
    return max(collected - refunded, ZERO)


def allocation(lead) -> dict[int, Decimal]:
    """The prepaid share of each trip, by reservation pk: trips in pickup order, each taking
    up to its `booked_total` (0 when it was added after booking or never frozen). Cancelled
    trips take nothing — their money is the next trip's, or a refund's."""
    pool = order_prepaid(lead)
    # An undated trip (a quote still being built) sorts last; it has no booked total anyway.
    trips = sorted(
        lead.reservations.all(),
        key=lambda r: (r.pickup_date or date.max, r.pickup_time or time.max, r.pk),
    )
    shares: dict[int, Decimal] = {}
    for trip in trips:
        if trip.is_cancelled:
            shares[trip.pk] = ZERO
            continue
        take = min(pool, Decimal(trip.booked_total or 0))
        shares[trip.pk] = take.quantize(_CENTS)
        pool -= take
    return shares


def money_for(trip) -> TripMoney:
    """The trip's live total against everything already paid for it."""
    own = Charge.objects.filter(reservation=trip, status=Charge.Status.SUCCEEDED)
    return TripMoney(
        line_total=trip.line_total,
        prepaid=allocation(trip.lead).get(trip.pk, ZERO),
        charged=_sum(own.filter(kind=Charge.Kind.TRIP)),
        refunded=_sum(own.filter(kind=Charge.Kind.REFUND)),
    )
