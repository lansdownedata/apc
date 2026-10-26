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

import stripe
from django.db import transaction
from django.db.models import Sum

from apps.notifications.models import Notification
from apps.reservations.models import TripExtra

from . import services
from .models import Charge, PaymentPlan

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
    up to its `booked_total` (0 when it was added after booking or never frozen). A plain
    cancellation takes nothing — its money is the next trip's, or a refund's. A no-show, a
    late cancel or a trip the affiliate dropped keeps its share: that is exactly the money
    processing settles (spec §4.1)."""
    from apps.reservations.models import Reservation

    nothing = (Reservation.TripStatus.CANCELLED, Reservation.TripStatus.COVID_CANCELLATION)
    pool = order_prepaid(lead)
    # An undated trip (a quote still being built) sorts last; it has no booked total anyway.
    trips = sorted(
        lead.reservations.all(),
        key=lambda r: (r.pickup_date or date.max, r.pickup_time or time.max, r.pk),
    )
    shares: dict[int, Decimal] = {}
    for trip in trips:
        if trip.trip_status in nothing:
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


# --- the two money actions ---------------------------------------------------------------


def _positive(amount) -> Decimal:
    amount = Decimal(amount).quantize(_CENTS)
    if amount <= ZERO:
        raise services.PaymentError("The amount has to be above zero.")
    return amount


def charge_trip(trip, amount, *, user=None, note: str = "") -> Charge:
    """Charge `amount` for this trip to the card on file, off-session.

    Under a lock on the plan, so a double-click makes one Charge; a retry that finds a
    PENDING off-session attempt for this trip and amount re-sends it under the same Stripe
    idempotency key (Stripe replays the first result). A decline records a FAILED charge and
    alerts — the booked balance's own status is left alone — and raises `PaymentError`.
    Success reconciles through `record_payment`, like the pay link does."""
    amount = _positive(amount)
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(lead=trip.lead)
        if not plan.stripe_payment_method_id or not plan.stripe_customer_id:
            raise services.PaymentError("No card on file. Send a payment link instead.")
        open_links = list(
            plan.charges.filter(
                kind=Charge.Kind.TRIP, reservation=trip, status=Charge.Status.PENDING
            ).exclude(stripe_client_secret="")
        )
        charge = plan.charges.filter(
            kind=Charge.Kind.TRIP,
            reservation=trip,
            status=Charge.Status.PENDING,
            amount=amount,
            stripe_client_secret="",
        ).first() or plan.record_charge(
            kind=Charge.Kind.TRIP,
            amount=amount,
            reservation=trip,
            note=note[:255],
            idempotency_key=_trip_key(plan, trip),
        )
    # A pay link already sent for this trip must not stay confirmable once the card is
    # charged here — that would collect the trip twice. Cancelled at Stripe, failed here.
    for link in open_links:
        _cancel_intent(link)
    try:
        intent = services._stripe().PaymentIntent.create(
            amount=services._cents(amount),
            currency="usd",
            customer=plan.stripe_customer_id,
            payment_method=plan.stripe_payment_method_id,
            payment_method_types=["card"],
            off_session=True,
            confirm=True,
            metadata={
                "lead_id": str(plan.lead_id),
                "kind": Charge.Kind.TRIP.value,
                "charge_id": str(charge.pk),
                "reservation_id": str(trip.pk),
            },
            idempotency_key=charge.idempotency_key,
        )
    except (stripe.error.CardError, stripe.error.InvalidRequestError) as exc:
        _record_trip_failure(plan, charge, exc)
        raise services.PaymentError(charge.failure_reason) from exc
    charge.stripe_payment_intent_id = intent.id
    charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    return services.record_payment(plan, intent.id, kind=Charge.Kind.TRIP)


def _trip_key(plan: PaymentPlan, trip) -> str:
    """`plan{pk}-trip-res{pk}-{attempt}` (spec §4.4): attempts count per trip."""
    attempt = plan.charges.filter(kind=Charge.Kind.TRIP, reservation=trip).count() + 1
    return f"plan{plan.pk}-trip-res{trip.pk}-{attempt}"


def _cancel_intent(charge: Charge) -> None:
    try:
        services._stripe().PaymentIntent.cancel(charge.stripe_payment_intent_id)
    except stripe.error.StripeError:
        # Already confirmed or already gone: Stripe knows; the webhook or reconcile sweep
        # tells us. Either way this row is no longer the one to reuse.
        pass
    charge.status = Charge.Status.FAILED
    charge.failure_reason = "Superseded by a card charge"
    charge.save(update_fields=["status", "failure_reason", "updated_at"])


def open_trip_intent(trip, amount, *, note: str = "") -> tuple[Charge, str]:
    """The pay-link side: an on-session intent for this trip that the customer's pay page
    confirms. Reused while open."""
    amount = _positive(amount)
    plan = services.ensure_plan(trip.lead)
    return services.open_intent_for(
        plan, kind=Charge.Kind.TRIP, amount=amount, reservation=trip, note=note[:255]
    )


def _record_trip_failure(plan: PaymentPlan, charge: Charge, exc: Exception) -> Charge:
    reason = (getattr(exc, "user_message", None) or str(exc))[:255]
    charge.status = Charge.Status.FAILED
    charge.failure_reason = reason
    charge.save(update_fields=["status", "failure_reason", "updated_at"])
    plan.fail_reason = reason
    plan.save(update_fields=["fail_reason", "updated_at"])
    lead = plan.lead
    lead.has_alert = True
    lead.save(update_fields=["has_alert", "updated_at"])
    Notification.notify(
        lead, Notification.Kind.BALANCE_FAILED, title="Trip charge failed", detail=reason
    )
    return charge


def refund_trip(trip, amount, *, reason: str, user=None) -> Decimal:
    """Return `amount` to the customer for this trip: its own charges first, then the
    order's prepaid money newest first. Every REFUND row carries the trip and the reason,
    and the trip gets a credit line for the same amount so `line_total` keeps telling the
    truth — afterwards the trip owes nothing more and nothing less (spec §4.7)."""
    amount = _positive(amount)
    reason = (reason or "").strip()
    if not reason:
        raise services.PaymentError("Say why the customer is refunded.")
    money = money_for(trip)
    if amount > money.allocated:
        raise services.PaymentError(
            f"Only ${money.allocated:,.2f} was paid for this trip; refund at most that."
        )
    plan = trip.lead.payment
    refundable = services.refundable_total(plan, trip=trip)
    if amount > refundable:
        raise services.PaymentError(f"Only ${refundable:,.2f} is still refundable to the card.")
    refunded = services.refund_payment(plan, amount, reservation=trip, note=reason, user=user)
    # The price may already say this money is owed back (a credit line or a lower rate
    # made `difference` negative): that part needs no second credit. Anything refunded
    # beyond it is a courtesy the price must show, so the trip owes nothing more or less.
    already_owed_back = max(ZERO, -money.difference)
    credit = refunded - min(refunded, already_owed_back)
    if credit > ZERO:
        TripExtra.objects.create(
            reservation=trip,
            kind=TripExtra.Kind.CREDIT,
            label=f"Refund: {reason}"[:80],
            amount=credit,
            created_by=user,
        )
    return refunded
