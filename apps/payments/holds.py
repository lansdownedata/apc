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

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import stripe
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone

from apps.notifications.models import Notification

from . import services, trip_money
from .models import Charge, PaymentPlan

logger = logging.getLogger(__name__)

TASK_KIND = "cleaning_hold_failed"
UNBOOKED_TASK_KIND = "cleaning_hold_unbooked"


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
    """Place the order's one cleaning hold on the card on file. Returns the hold row, or
    None for an order that was never held or has no card (the task opens once).

    Idempotent. An order that already has a hold gets that row back in any settled state,
    even after its held trips were cancelled, and a decline (FAILED) is never retried. A
    row still PENDING is finished instead: with no intent id (Stripe was unreachable) the
    create is re-sent under the same idempotency key, which Stripe replays; with one (we
    stopped between Stripe's reply and the save) the intent is read back. A Stripe outage
    never raises out of here, so the cron carries on with the next order."""
    now = now or timezone.now()
    if not eligible(lead):
        return any_hold(lead)
    services.ensure_plan(lead)
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(lead=lead)
        charge = plan.charges.filter(kind=Charge.Kind.HOLD).order_by("-pk").first()
        if charge is not None and charge.status != Charge.Status.PENDING:
            return charge
        has_card = bool(plan.stripe_payment_method_id and plan.stripe_customer_id)
        if charge is None and has_card:
            charge = plan.record_charge(
                kind=Charge.Kind.HOLD, amount=hold_amount(lead), note="Cleaning hold"
            )
    if charge is None:
        _hold_failed(lead, "Cleaning hold not placed: no card on file", "no card on file")
        return None

    try:
        if charge.stripe_payment_intent_id:
            intent = services._stripe().PaymentIntent.retrieve(
                charge.stripe_payment_intent_id, expand=["latest_charge"]
            )
        else:
            intent = _create_intent(plan, charge)
    except (stripe.error.CardError, stripe.error.InvalidRequestError) as exc:
        # The card said no, or the saved card is gone: a decline, recorded and not retried.
        reason = (getattr(exc, "user_message", None) or str(exc))[:255]
        return _fail(lead, charge, reason, f"Cleaning hold declined: {reason}")
    except stripe.error.IdempotencyError as exc:
        # The key was reused for a different request: the re-send can never finish.
        logger.error("cleaning hold: idempotency conflict for charge %s: %s", charge.pk, exc)
        return _unplaceable(lead, charge, "idempotency conflict")
    except stripe.error.StripeError:
        logger.exception("cleaning hold: Stripe unreachable for charge %s", charge.pk)
        return charge

    if charge.stripe_payment_intent_id != intent.id:
        charge.stripe_payment_intent_id = intent.id
        charge.save(update_fields=["stripe_payment_intent_id", "updated_at"])
    state = getattr(intent, "status", "")
    if state in ("canceled", "succeeded"):
        # Not a hold any more, and nothing this job can turn back into one.
        logger.error("cleaning hold: charge %s intent %s is %s", charge.pk, intent.id, state)
        return _unplaceable(lead, charge, state)
    if state != "requires_capture":
        logger.warning(
            "cleaning hold: charge %s intent %s is %s, not awaiting capture",
            charge.pk,
            intent.id,
            getattr(intent, "status", ""),
        )
        return charge
    charge.status = Charge.Status.AUTHORIZED
    charge.authorized_at = now
    charge.capture_expires_at = _capture_before(intent) or now + timedelta(
        days=services.AUTH_HOLD_DAYS
    )
    charge.save(update_fields=["status", "authorized_at", "capture_expires_at", "updated_at"])
    services._snapshot_plan_card(plan, charge)
    return charge


def _fail(lead, charge: Charge, reason: str, title: str) -> Charge:
    """Record the hold FAILED (the cron stops trying it) and tell staff, once."""
    charge.status = Charge.Status.FAILED
    charge.failure_reason = reason[:255]
    charge.save(update_fields=["status", "failure_reason", "updated_at"])
    _hold_failed(lead, title, reason[:255])
    return charge


def _unplaceable(lead, charge: Charge, state: str) -> Charge:
    return _fail(lead, charge, state, f"Cleaning hold could not be placed: {state}")


def _create_intent(plan: PaymentPlan, charge: Charge):
    """The off-session, manual-capture authorization for `charge`, under its own
    idempotency key (a re-send replays Stripe's first answer)."""
    return services._stripe().PaymentIntent.create(
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
    One capture per hold: anything but an AUTHORIZED hold raises `PaymentError`, and so does
    a hold the bank already released (it is marked EXPIRED; charge the card instead)."""
    lead = trip.lead
    intent = None
    with transaction.atomic():
        plan = PaymentPlan.objects.select_for_update().get(lead=lead)
        # The row itself, held across Stripe's capture and our save: a `succeeded` webhook
        # for this intent locks the same row, so it waits and then sees the trip (APC-126).
        hold = (
            plan.charges.select_for_update()
            .filter(kind=Charge.Kind.HOLD)
            .exclude(status=Charge.Status.FAILED)
            .order_by("-pk")
            .first()
        )
        if hold is None or hold.status != Charge.Status.AUTHORIZED:
            raise services.PaymentError("No cleaning hold to capture.")
        if hold.captured_at is None:
            take = min(Decimal(amount), Decimal(hold.amount)).quantize(Decimal("0.01"))
            if take <= services.ZERO:
                raise services.PaymentError("Nothing to capture from the cleaning hold.")
            intent, recovered = _capture(hold, take)
            expired = intent is None
            if recovered and not expired:
                # Our reply was lost: what Stripe took is the fact, not what we asked for.
                take = (Decimal(intent.amount_received) / 100).quantize(Decimal("0.01"))
            if not expired:
                hold.captured_at = timezone.now()
                _tag(hold, trip, take)
        else:
            # Captured at Stripe, books not finished: by a call of ours that stopped before
            # them, or seen by the webhook with no trip to book it against. This trip
            # adopts it, for what Stripe took.
            expired = False
            if hold.reservation_id is None:
                _tag(hold, trip, Decimal(hold.amount))
    if expired:
        mark_expired(hold)
        raise services.PaymentError("The bank released the cleaning hold; charge the card instead.")
    # The one reconcile every money path shares: SUCCEEDED and the ledger entry, once.
    return services.record_payment(
        plan, hold.stripe_payment_intent_id, kind=Charge.Kind.TRIP, intent=intent
    )


def _tag(hold: Charge, trip, amount: Decimal) -> None:
    """The captured hold is this trip's cleaning fee, for `amount`."""
    hold.reservation = trip
    hold.amount = amount
    hold.note = "Cleaning fee from the hold"
    hold.receipt_lines = trip_money.charge_lines(trip, amount, lines=[("Cleaning fee", amount)])
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


def record_captured(plan: PaymentPlan, intent) -> Charge | None:
    """`payment_intent.succeeded` for a hold. A capture our own call already tagged with its
    trip is booked here (the same `record_payment`, once). One with no trip yet (our reply
    was lost, or someone captured it at Stripe) is only marked captured, for what Stripe
    took: it stays AUTHORIZED and unbooked until `capture_hold` adopts it for a trip, so
    nothing books money against no trip or for more than was taken."""
    pi_id = intent["id"]
    with transaction.atomic():
        hold = (
            plan.charges.select_for_update()
            .filter(kind=Charge.Kind.HOLD, stripe_payment_intent_id=pi_id)
            .first()
        )
        if hold is None or hold.status != Charge.Status.AUTHORIZED:
            return hold
        if hold.reservation_id is None:
            if hold.captured_at is None:
                received = intent.get("amount_received") or 0
                hold.captured_at = timezone.now()
                if received:
                    hold.amount = (Decimal(received) / 100).quantize(Decimal("0.01"))
                hold.save(update_fields=["captured_at", "amount", "updated_at"])
            return hold
    return services.record_payment(plan, pi_id, kind=Charge.Kind.HOLD)


def _capture(hold: Charge, take: Decimal) -> tuple[object | None, bool]:
    """Capture `take` from the hold: (intent, whether it was read back). A lost reply is
    resolved by reading the intent: captured → that intent; released by the bank → None;
    anything else → `PaymentError`."""
    try:
        intent = services._stripe().PaymentIntent.capture(
            hold.stripe_payment_intent_id,
            amount_to_capture=services._cents(take),
            idempotency_key=f"{hold.idempotency_key}-capture",
        )
        return intent, False
    except stripe.error.StripeError as exc:
        try:
            intent = services._stripe().PaymentIntent.retrieve(hold.stripe_payment_intent_id)
        except stripe.error.StripeError:
            intent = None
        status = getattr(intent, "status", "") if intent is not None else ""
        if status == "succeeded":
            return intent, True
        if status == "canceled":
            return None, True
        raise services.PaymentError(
            f"The cleaning hold could not be captured: {getattr(exc, 'user_message', None) or exc}"
        ) from exc


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


# --- the hourly job (`cleaning-holds`) -------------------------------------------------------


def _zone(trip) -> ZoneInfo:
    """The trip's own zone (blank or unknown: the business zone)."""
    try:
        return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _today(trip, now: datetime) -> date:
    """`now`'s date where the trip is."""
    return now.astimezone(_zone(trip)).date()


def _live_held(lead) -> list:
    """`held_trips` read from the lead's prefetched reservations (no query), in any order."""
    from apps.dispatch.selectors import CANCELLED_STATUSES

    return [
        t
        for t in lead.reservations.all()
        if t.trip_status not in CANCELLED_STATUSES and t.vehicle_id and t.vehicle.cleaning_fee > 0
    ]


def _dated(trips: list) -> list:
    """The trips with a pickup day, in pickup order: the job's clock reads their days."""
    dated = [t for t in trips if t.pickup_date is not None]
    return sorted(dated, key=lambda t: (t.pickup_date, t.pickup_time or datetime.min.time(), t.pk))


def _over(trips: list, now: datetime, release_days: int) -> bool:
    """More than `release_days` days have passed since the last held trip's day, in its
    zone: nothing is left for a hold to cover."""
    last = trips[-1]
    return last.pickup_date + timedelta(days=release_days) < _today(last, now)


def _with_trips(queryset, prefix: str = ""):
    from apps.reservations.models import Reservation

    return queryset.prefetch_related(
        Prefetch(f"{prefix}reservations", queryset=Reservation.objects.select_related("vehicle"))
    )


def due_for_placement(now: datetime) -> list:
    """Booked card orders with a held trip and no HOLD row yet (in any state) whose
    placement moment has come: `hold_place_time` on the first held trip's day, in that
    trip's zone (or any time after it), unless the order is already over (`_over`).

    The database narrows by date across every zone at once (local dates sit within a day of
    UTC's); the exact rule is checked per order in Python. A fixed handful of queries."""
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead
    from apps.reservations.models import Reservation
    from apps.tasks.models import TaskConfig

    config = TaskConfig.load()
    latest = (now + timedelta(days=1)).date()
    earliest = (now - timedelta(days=1)).date() - timedelta(days=config.hold_release_days)
    held = Reservation.objects.filter(lead=OuterRef("pk"), vehicle__cleaning_fee__gt=0).exclude(
        trip_status__in=CANCELLED_STATUSES
    )
    leads = _with_trips(
        Lead.objects.filter(status=Lead.Status.BOOKED, billing_group__isnull=True)
        .filter(Exists(held.filter(pickup_date__lte=latest)))
        .filter(Exists(held.filter(pickup_date__gte=earliest)))
        .exclude(Exists(Charge.objects.filter(plan__lead=OuterRef("pk"), kind=Charge.Kind.HOLD)))
        .select_related("contact__company")
        .order_by("pk")
    )
    due, seen = [], set()
    for lead in leads:
        # Once seen locally (MySQL 8.0, fresh test tables), the semi-join gave a lead once
        # per matching trip; a lead is placed once whatever plan the database picks.
        if lead.pk in seen:
            continue
        seen.add(lead.pk)
        trips = _dated(_live_held(lead))
        if not trips or lead.is_farm_in or _over(trips, now, config.hold_release_days):
            continue
        first = trips[0]
        if now >= datetime.combine(first.pickup_date, config.hold_place_time, _zone(first)):
            due.append(lead)
    return due


def sweep(now: datetime) -> int:
    """Settle every hold still open (PENDING or AUTHORIZED). Returns how many changed or
    were flagged. One order's failure is logged and the sweep carries on."""
    from apps.tasks.models import TaskConfig

    release_days = TaskConfig.load().hold_release_days
    rows = _with_trips(
        Charge.objects.filter(
            kind=Charge.Kind.HOLD, status__in=(Charge.Status.PENDING, Charge.Status.AUTHORIZED)
        )
        .select_related("plan__lead__contact__company")
        .order_by("pk"),
        prefix="plan__lead__",
    )
    swept = 0
    for hold in rows:
        try:
            swept += _sweep_one(hold, now, release_days)
        except Exception:
            logger.exception("cleaning hold: sweep failed for charge %s", hold.pk)
    return swept


def _sweep_one(hold: Charge, now: datetime, release_days: int) -> int:
    from apps.leads.models import Lead

    lead = hold.plan.lead
    live = _live_held(lead)
    # A hold serves nothing once the order is lost or no held trip is left on it.
    needed = lead.status == Lead.Status.BOOKED and bool(live)
    if hold.status == Charge.Status.PENDING:
        return _settle_pending(lead, hold, now, needed)
    if hold.captured_at is not None:
        # Money moved at Stripe: never released or expired. If no trip took it up, the
        # books are missing it, and a person books it.
        return _flag_unbooked(lead) if hold.reservation_id is None else 0
    if hold.capture_expires_at is not None and hold.capture_expires_at < now:
        return int(mark_expired(hold).status != Charge.Status.AUTHORIZED)
    trips = _dated(live)
    if not needed:
        reason = "order cancelled" if lead.status != Lead.Status.BOOKED else "no held trip left"
    elif trips and _over(trips, now, release_days):
        reason = "unused after the last held trip"
    else:
        return 0
    # RELEASED, or EXPIRED when the bank had let it go first: either way, settled.
    return int(release_hold(lead, reason=reason).status != Charge.Status.AUTHORIZED)


def _settle_pending(lead, hold: Charge, now: datetime, needed: bool) -> int:
    """A hold Stripe has not finished answering for. With nothing left to hold for, an
    intent Stripe created is cancelled; otherwise `place_hold` re-sends or reads it back,
    and one still unanswered after the card-hold window is given up as FAILED."""
    if not needed and hold.stripe_payment_intent_id:
        try:
            services._stripe().PaymentIntent.cancel(hold.stripe_payment_intent_id)
        except stripe.error.StripeError:
            if _stripe_status(hold) != "canceled":
                logger.exception("cleaning hold: could not cancel pending charge %s", hold.pk)
                return 0
        hold.status = Charge.Status.RELEASED
        hold.note = "no held trip left"
        hold.save(update_fields=["status", "note", "updated_at"])
        return 1
    place_hold(lead, now=now)
    hold.refresh_from_db()
    if hold.status != Charge.Status.PENDING:
        return 1
    if hold.created_at < now - timedelta(days=services.AUTH_HOLD_DAYS):
        _unplaceable(lead, hold, "no answer from Stripe")
        return 1
    return 0


def _flag_unbooked(lead) -> int:
    """One open Accounting task (and a notification) for a captured hold no trip took up;
    none while one is still open."""
    from apps.tasks import services as tasks
    from apps.tasks.models import Task

    if Task.objects.filter(lead=lead, kind=UNBOOKED_TASK_KIND, status=Task.Status.OPEN).exists():
        return 0
    title = "Cleaning hold captured but not booked to a trip"
    task = tasks.open_request_task(lead, UNBOOKED_TASK_KIND)
    task.note = title
    task.save(update_fields=["note", "updated_at"])
    Notification.notify(
        lead,
        Notification.Kind.HOLD_UNBOOKED,
        title=title,
        detail="Process the trip with its cleaning fee to book it.",
    )
    lead.has_alert = True
    lead.save(update_fields=["has_alert", "updated_at"])
    return 1


def run_cron(now: datetime | None = None) -> int:
    """The hourly `cleaning-holds` job: the sweep, then placement. Nothing at all while
    `hold_enabled` is off. Each order on its own: one failure is logged, never stops the
    run. Returns holds swept plus holds placed (AUTHORIZED this run)."""
    from apps.tasks.models import TaskConfig

    if not TaskConfig.load().hold_enabled:
        return 0
    now = now or timezone.now()
    # Sweep first, so a hold this run leaves PENDING waits for the next run's retry.
    processed = sweep(now)
    for lead in due_for_placement(now):
        try:
            hold = place_hold(lead, now=now)
        except Exception:
            logger.exception("cleaning hold: placement failed for lead %s", lead.pk)
            continue
        if hold is not None and hold.status == Charge.Status.AUTHORIZED:
            processed += 1
    return processed
