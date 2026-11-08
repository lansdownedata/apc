"""The cleaning hold (APC-95, spec 2026-10-27 §2, §4).

One manual-capture card authorization per held order, stored as a `Charge` of kind HOLD on
the order's plan: placed off-session for the highest cleaning fee among the order's held
trips, captured once for a trip that needed cleaning, released when no held trip is left to
need it. A hold is never money: placing, releasing or expiring it posts nothing to the books
and leaves the plan's deposit and balance alone. Only a capture moves money, and it posts
exactly like a trip charge (`services.record_payment`).

A card problem never raises out of here: a missing card or a decline opens one
`cleaning_hold_failed` task, notifies staff and is not retried. The trip runs regardless.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import stripe
from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Notification

from . import services, trip_money
from .models import Charge, PaymentPlan

TASK_KIND = "cleaning_hold_failed"


# --- which orders are held ------------------------------------------------------------------


def held_trips(lead) -> list:
    """The order's non-cancelled trips whose vehicle type carries a cleaning fee, in pickup
    order."""
    from apps.dispatch.selectors import CANCELLED_STATUSES

    return list(
        lead.reservations.exclude(trip_status__in=CANCELLED_STATUSES)
        .filter(vehicle__cleaning_fee__gt=0)
        .select_related("vehicle")
        .order_by("pickup_date", "pickup_time", "pk")
    )


def eligible(lead) -> bool:
    """Booked, a card order (not farmed in, not billed to an account), with a held trip."""
    from apps.leads.models import Lead

    return (
        lead.status == Lead.Status.BOOKED
        and not lead.is_farm_in
        and not lead.on_account
        and bool(held_trips(lead))
    )


def hold_amount(lead) -> Decimal:
    """The highest cleaning fee among the held trips (not the sum): one hold covers one
    capture."""
    return max((Decimal(t.vehicle.cleaning_fee) for t in held_trips(lead)), default=Decimal("0"))


# --- the row ---------------------------------------------------------------------------------


def _holds(lead):
    plan = PaymentPlan.objects.filter(lead=lead).first()
    if plan is None:
        return Charge.objects.none()
    return plan.charges.filter(kind=Charge.Kind.HOLD).order_by("-pk")


def current_hold(lead) -> Charge | None:
    """The order's hold in any state but FAILED (a decline is no hold), or None."""
    return _holds(lead).exclude(status=Charge.Status.FAILED).first()


def any_hold(lead) -> Charge | None:
    """The order's latest HOLD row in any state, FAILED included."""
    return _holds(lead).first()


# --- placing ---------------------------------------------------------------------------------


def place_hold(lead, *, now=None) -> Charge | None:
    """Place the order's one cleaning hold on the card on file. Idempotent: an order that
    already has a HOLD row in any state gets that row back, so a decline is never retried.
    Returns None for an order that isn't held, or that has no card (the task opens once)."""
    now = now or timezone.now()
    if not eligible(lead):
        return any_hold(lead)
    services.ensure_plan(lead)
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(lead=lead)
        existing = plan.charges.filter(kind=Charge.Kind.HOLD).order_by("-pk").first()
        if existing is not None:
            return existing
        has_card = bool(plan.stripe_payment_method_id and plan.stripe_customer_id)
        if has_card:
            charge = plan.record_charge(
                kind=Charge.Kind.HOLD, amount=hold_amount(lead), note="Cleaning hold"
            )
    if not has_card:
        _hold_failed(lead, "Cleaning hold not placed: no card on file", "no card on file")
        return None

    try:
        intent = services._stripe().PaymentIntent.create(
            amount=services._cents(charge.amount),
            currency="usd",
            customer=plan.stripe_customer_id,
            payment_method=plan.stripe_payment_method_id,
            payment_method_types=["card"],
            off_session=True,
            confirm=True,
            capture_method="manual",
            # The charge carries the issuer's real capture deadline.
            expand=["latest_charge"],
            metadata={
                "lead_id": str(plan.lead_id),
                "kind": Charge.Kind.HOLD.value,
                "charge_id": str(charge.pk),
            },
            idempotency_key=charge.idempotency_key,
        )
    except stripe.error.StripeError as exc:
        reason = (getattr(exc, "user_message", None) or str(exc))[:255]
        charge.status = Charge.Status.FAILED
        charge.failure_reason = reason
        charge.save(update_fields=["status", "failure_reason", "updated_at"])
        _hold_failed(lead, f"Cleaning hold declined: {reason}", reason)
        return charge

    charge.status = Charge.Status.AUTHORIZED
    charge.stripe_payment_intent_id = intent.id
    charge.authorized_at = now
    charge.capture_expires_at = _capture_before(intent) or now + timedelta(
        days=services.AUTH_HOLD_DAYS
    )
    charge.save(
        update_fields=[
            "status",
            "stripe_payment_intent_id",
            "authorized_at",
            "capture_expires_at",
            "updated_at",
        ]
    )
    services._snapshot_plan_card(plan, charge)
    return charge


def _field(obj, name):
    """Stripe objects read both ways; a plain dict only by key."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _capture_before(intent) -> datetime | None:
    """The issuer's capture deadline, from the expanded charge's card details, when Stripe
    gave one (epoch seconds)."""
    charge = _field(intent, "latest_charge")
    details = _field(charge, "payment_method_details") if charge is not None else None
    card = _field(details, "card") if details is not None else None
    stamp = _field(card, "capture_before") if card is not None else None
    if isinstance(stamp, int) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, tz=UTC)
    return None


def _hold_failed(lead, title: str, reason: str) -> None:
    """The hold could not be placed: one task for Customer Service (its note is `title`),
    a staff notification and the order's alert. Once per order."""
    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    if Task.objects.filter(lead=lead, kind=TASK_KIND).exists():
        return
    task = tasks.open_request_task(lead, TASK_KIND)
    task.note = title
    task.save(update_fields=["note", "updated_at"])
    Notification.notify(lead, Notification.Kind.HOLD_FAILED, title=title, detail=reason)
    lead.has_alert = True
    lead.save(update_fields=["has_alert", "updated_at"])


# --- capturing, releasing, expiry ------------------------------------------------------------


def capture_hold(trip, amount) -> Charge:
    """Take the cleaning fee for `trip` out of the hold: `min(amount, hold)` is captured
    (Stripe lets the rest go), the row is tagged with the trip and posted like a trip charge.
    One capture per hold: anything but an AUTHORIZED hold raises `PaymentError`."""
    lead = trip.lead
    intent = None
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(lead=lead)
        hold = current_hold(lead)
        if hold is None or hold.status != Charge.Status.AUTHORIZED:
            raise services.PaymentError("No cleaning hold to capture.")
        if hold.captured_at is None:
            take = min(Decimal(amount), Decimal(hold.amount)).quantize(Decimal("0.01"))
            if take <= services.ZERO:
                raise services.PaymentError("Nothing to capture from the cleaning hold.")
            try:
                intent = services._stripe().PaymentIntent.capture(
                    hold.stripe_payment_intent_id, amount_to_capture=services._cents(take)
                )
            except stripe.error.StripeError as exc:
                raise services.PaymentError(
                    f"The cleaning hold could not be captured: "
                    f"{getattr(exc, 'user_message', None) or exc}"
                ) from exc
            hold.reservation = trip
            hold.amount = take
            hold.captured_at = timezone.now()
            hold.note = "Cleaning fee from the hold"
            hold.receipt_lines = trip_money.charge_lines(trip, take, lines=[("Cleaning fee", take)])
            hold.save(
                update_fields=[
                    "reservation",
                    "amount",
                    "captured_at",
                    "note",
                    "receipt_lines",
                    "updated_at",
                ]
            )
        # else: captured at Stripe by a call that stopped before the books; finish it below.
    # The one reconcile every money path shares: SUCCEEDED and the ledger entry, once.
    return services.record_payment(
        plan, hold.stripe_payment_intent_id, kind=Charge.Kind.TRIP, intent=intent
    )


def release_hold(lead, *, reason: str = "") -> Charge | None:
    """Let an AUTHORIZED hold go (Stripe cancel). Any other state is returned untouched, so
    calling it twice cancels once. None when the order has no hold."""
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().filter(lead=lead).first()
        if plan is None:
            return None
        hold = current_hold(lead)
        if hold is None or hold.status != Charge.Status.AUTHORIZED or hold.captured_at:
            return hold
        try:
            services._stripe().PaymentIntent.cancel(hold.stripe_payment_intent_id)
        except stripe.error.StripeError as exc:
            if _stripe_status(hold) != "canceled":
                raise services.PaymentError(
                    "Stripe couldn't release the cleaning hold. Try again."
                ) from exc
            # The bank let it go before we did: that is an expiry, not our release.
            hold.status = Charge.Status.EXPIRED
            hold.save(update_fields=["status", "updated_at"])
            return hold
        hold.status = Charge.Status.RELEASED
        fields = ["status", "updated_at"]
        if reason:
            hold.note = reason[:255]
            fields.append("note")
        hold.save(update_fields=fields)
    return hold


def _stripe_status(hold: Charge) -> str:
    """The intent's status at Stripe, or "" when Stripe can't be reached either."""
    try:
        intent = services._stripe().PaymentIntent.retrieve(hold.stripe_payment_intent_id)
    except stripe.error.StripeError:
        return ""
    return getattr(intent, "status", "") or ""


def mark_expired(charge: Charge) -> Charge:
    """The issuer dropped the hold first: AUTHORIZED becomes EXPIRED; anything else (one we
    released or captured ourselves) is left as it is. Re-read first, like
    `services.expire_authorization`."""
    charge.refresh_from_db()
    if charge.status == Charge.Status.AUTHORIZED and charge.captured_at is None:
        charge.status = Charge.Status.EXPIRED
        charge.save(update_fields=["status", "updated_at"])
    return charge
