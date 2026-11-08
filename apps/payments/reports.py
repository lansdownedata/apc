"""Internal reports (staff email) — read-only queries over orders and plans."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Min, Q
from django.urls import reverse
from django.utils import timezone

from apps.leads.models import Lead

from . import ledger
from .models import Charge, PaymentPlan

# Urgency tiers for the hold countdown (APC-26). Display-only for now — step 3 moves them
# onto the settings singleton alongside the auth-expiry alert thresholds, so the numbers
# staff see and the numbers that page someone can't drift apart.
AUTH_WARN_HOURS = 48
AUTH_CRITICAL_HOURS = 12


def order_balances_with_remaining(lead: Lead) -> dict:
    """The order's ledger balances plus what the customer still owes.

    The money card reads both, and it is rendered by the quote workspace and the order
    page — so this lives here rather than being a private helper in one of their views.
    """
    from . import services

    balances = ledger.order_balances(lead)
    balances["remaining"] = services.remaining_balance(lead)
    return balances


def hold_tier(hours_left: float | None) -> str:
    """ "" / warning / critical — the same vocabulary as a DispatchException tier, so the
    board and the orders console mean the same thing by an amber row. A hold with no
    recorded deadline has no urgency to report."""
    if hours_left is None:
        return ""
    if hours_left <= AUTH_CRITICAL_HOURS:
        return "critical"
    if hours_left <= AUTH_WARN_HOURS:
        return "warning"
    return ""


def _held_deposit(charges) -> Charge | None:
    """The authorized-but-uncaptured deposit among an already-loaded set of charges.

    Iterates rather than filters so a prefetched `plan.charges.all()` stays a single query.
    """
    return next(
        (
            c
            for c in charges
            if c.kind == Charge.Kind.DEPOSIT and c.status == Charge.Status.AUTHORIZED
        ),
        None,
    )


def _hours_left(expires, now) -> float | None:
    return (expires - now).total_seconds() / 3600 if expires else None


def awaiting_confirmation_rows(now=None) -> list[dict]:
    """Engaged orders — deposit authorized, waiting on APC to confirm or cancel (APC-26).

    Sorted by hold expiry ascending: the thing about to lapse is the thing to do next.
    An order whose deadline has already passed sorts first and reads critical — the sweep
    in step 3 is what actually resolves it, but staff should see it before then.
    """
    now = now or timezone.now()
    leads = (
        Lead.objects.filter(status=Lead.Status.ENGAGED)
        # `trip_count`, not the `reservation_count` property — that property is a COUNT
        # per row, and this list is rendered on the dashboard and the orders console.
        .annotate(earliest=Min("reservations__pickup_date"), trip_count=Count("reservations"))
        .select_related("contact", "payment")
        .prefetch_related("payment__charges")
    )
    rows = []
    for lead in leads:
        plan = getattr(lead, "payment", None)
        if plan is None:
            continue
        charge = _held_deposit(plan.charges.all())
        if charge is None:
            continue
        expires = charge.capture_expires_at
        hours_left = _hours_left(expires, now)
        rows.append(
            {
                "lead": lead,
                "plan": plan,
                "charge": charge,
                "quote_no": lead.quote_no,
                "customer": lead.contact.name,
                "trips": lead.trip_count,
                "earliest_pickup": lead.earliest,
                "held": charge.amount,
                "authorized_at": charge.authorized_at,
                "expires_at": expires,
                "hours_left": hours_left,
                "tier": hold_tier(hours_left),
            }
        )
    # None-expiry rows (shouldn't happen, but a missing deadline is not urgent) sort last.
    rows.sort(key=lambda r: (r["hours_left"] is None, r["hours_left"]))
    return rows


def awaiting_confirmation_summary(rows=None, now=None) -> dict:
    """Count + money on hold + the soonest deadline, for the console banner and the
    dashboard tile.

    Pass `rows` when the caller already has them (the orders console renders the queue and
    the banner off one pass) — otherwise this loads them itself for the dashboard.
    """
    if rows is None:
        rows = awaiting_confirmation_rows(now=now)
    soonest = rows[0]["hours_left"] if rows else None
    return {
        "count": len(rows),
        "held": sum((r["held"] for r in rows), Decimal("0.00")),
        "soonest_hours": soonest,
        "tier": hold_tier(soonest),
    }


def authorized_hold(lead, now=None) -> dict:
    """The held deposit on one engaged order, flattened for the quote workspace (APC-26).

    Blank keys rather than a nested None so the template can read them flat; every value is
    empty for the overwhelmingly common case of an order that isn't engaged.
    """
    blank = {
        "authorized_held": None,
        "authorized_at": None,
        "authorized_expires_at": None,
        "authorized_tier": "",
        "authorized_lapsed": False,
    }
    if lead.status != Lead.Status.ENGAGED:
        return blank
    plan = getattr(lead, "payment", None)
    if plan is None:
        return blank
    charge = _held_deposit(plan.charges.all())
    if charge is None:
        return blank
    hours_left = _hours_left(charge.capture_expires_at, now or timezone.now())
    return {
        "authorized_held": charge.amount,
        "authorized_at": charge.authorized_at,
        "authorized_expires_at": charge.capture_expires_at,
        "authorized_tier": hold_tier(hours_left),
        "authorized_lapsed": hours_left is not None and hours_left <= 0,
    }


def cleaning_hold(lead) -> dict:
    """The order's cleaning hold on the money card (APC-95, spec §5), flattened like
    `authorized_hold`: the amount, the card, when it was placed (in the first held trip's
    zone, with its abbreviation) and what became of it. `hold_state` is one of "placed",
    "pending", "released", "captured", "expired", "declined" (the card said no) and
    "not placed" (no card on file, or a hold Stripe would not make); `hold_detail` is the
    sentence after the dash. Every value is blank for an order with no hold.

    `hold_eligible` says whether the order is held at all, so the card can show the fee
    acknowledgment the held order's confirmation asks for."""
    from . import holds

    blank = {
        "hold_amount": None,
        "hold_card": "",
        "hold_placed_at": "",
        "hold_tz_label": "",
        "hold_state": "",
        "hold_detail": "",
    }
    eligible = holds.eligible(lead)
    hold = holds.any_hold(lead)
    if hold is None:
        if not eligible or not _hold_task_open(lead):
            return {**blank, "hold_eligible": eligible}
        # No card on file: no row was made, only the task.
        return {
            **blank,
            "hold_amount": holds.hold_amount(lead),
            "hold_state": "not placed",
            "hold_detail": "could not be placed: no card on file",
            "hold_eligible": eligible,
        }
    state, detail = _hold_outcome(hold)
    placed, tz_label = "", ""
    if hold.authorized_at is not None:
        placed, tz_label = _in_trip_zone(hold.authorized_at, _first_trip(lead))
    return {
        # A captured row carries what was taken; the line then leads with the capture.
        "hold_amount": None if state == "captured" else hold.amount,
        "hold_card": _card_label(hold),
        "hold_placed_at": placed,
        "hold_tz_label": tz_label,
        "hold_state": state,
        "hold_detail": detail,
        "hold_eligible": eligible,
    }


def _hold_task_open(lead) -> bool:
    from apps.tasks.models import Task

    from . import holds

    return Task.objects.filter(lead=lead, kind=holds.TASK_KIND).exists()


def _hold_outcome(hold: Charge) -> tuple[str, str]:
    from . import holds

    S = Charge.Status
    if hold.status == S.PENDING:
        return "pending", "waiting on Stripe"
    if hold.status == S.AUTHORIZED and hold.captured_at is None:
        return "placed", ""
    if hold.status == S.AUTHORIZED:
        return "captured", f"captured ${hold.amount:,.2f}, not yet booked to a trip"
    if hold.status == S.SUCCEEDED:
        trip = f" for trip {hold.reservation.reference}" if hold.reservation_id else ""
        return "captured", f"captured ${hold.amount:,.2f}{trip}"
    if hold.status == S.RELEASED:
        why = hold.note if hold.note and hold.note != "Cleaning hold" else ""
        return "released", f"released: {why}" if why else "released"
    if hold.status == S.EXPIRED:
        return "expired", "expired at the bank"
    reason = hold.failure_reason or "unknown reason"
    if hold.note == holds.DECLINED_NOTE:
        return "declined", f"declined: {reason}"
    return "not placed", f"could not be placed: {reason}"


def _first_trip(lead):
    """The trip whose zone the hold's times read in: the first held trip, else the
    order's first trip (its held trips may all be cancelled by now)."""
    from . import holds

    held = holds.held_trips(lead)
    if held:
        return held[0]
    return lead.reservations.order_by("pickup_date", "pickup_time", "pk").first()


def _in_trip_zone(moment, trip) -> tuple[str, str]:
    """`("Nov 8, 3:02 AM", "PST")`: `moment` on the trip's wall clock, and that zone's
    abbreviation at that moment. A blank or unknown zone is TIME_ZONE."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from django.utils import dateformat

    try:
        zone = ZoneInfo((trip.pickup_timezone if trip else "") or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo(settings.TIME_ZONE)
    local = moment.astimezone(zone)
    return dateformat.format(local, "M j, g:i A"), local.tzname() or ""


def _card_label(hold: Charge) -> str:
    """The card the hold sits on: the row's own snapshot, else the plan's card."""
    brand, last4 = hold.card_brand, hold.card_last4
    if not last4:
        brand, last4 = hold.plan.card_brand, hold.plan.card_last4
    return f"{brand.title()} ••{last4}" if last4 else ""


def unpaid_deposit_leads(today: date | None = None):
    """Booked orders whose deposit isn't paid and whose earliest pickup is inside the
    balance window (`today + BALANCE_CHARGE_DAYS_BEFORE`), annotated with `earliest`.
    A booked lead with no plan at all counts as unpaid; one billed to an account owes no
    deposit (APC-105) and never counts. The deposit report lists these;
    the exception dashboard (APC-69) counts them."""
    today = today or timezone.localdate()
    cutoff = today + timedelta(days=settings.BALANCE_CHARGE_DAYS_BEFORE)
    unpaid = ~Q(payment__deposit_status=PaymentPlan.DepositStatus.PAID)
    return (
        Lead.objects.filter(status=Lead.Status.BOOKED, billing_group__isnull=True)
        .filter(Q(payment__isnull=True) | unpaid)
        .annotate(earliest=Min("reservations__pickup_date"))
        .filter(earliest__isnull=False, earliest__lte=cutoff)
    )


def unpaid_deposit_rows(today: date | None = None) -> list[dict]:
    """`unpaid_deposit_leads` as report rows, soonest/overdue first.

    `collected` comes from the ledger, not the plan flags, so a partial card charge shows
    what actually arrived.
    """
    today = today or timezone.localdate()
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    leads = (
        unpaid_deposit_leads(today)
        .select_related("contact", "payment")
        .prefetch_related("reservations")
        .order_by("earliest", "pk")
    )
    rows = []
    for lead in leads:
        plan = getattr(lead, "payment", None)
        rows.append(
            {
                "lead": lead,
                "quote_no": lead.quote_no,
                "customer": lead.contact.name,
                "email": lead.contact.email or "",
                "phone": lead.contact.phone or "",
                "trips": lead.reservations.count(),
                "earliest_pickup": lead.earliest,
                "days_out": (lead.earliest - today).days,
                "overdue_days": max(0, (today - lead.earliest).days),
                "total": plan.quote_total if plan and plan.quote_total else lead.quote_total,
                "collected": ledger.order_balances(lead)["collected"],
                "url": f"{base}{reverse('lead_detail', args=[lead.pk])}",
            }
        )
    return rows
