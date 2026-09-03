"""Dispatch exception monitoring (APC-23).

`run_dispatch_monitor` walks upcoming booked trips every cron tick and raises a tiered
`DispatchException` whenever an expected milestone is overdue:

- **No coverage** — no confirmed driver / affiliate as pickup approaches.
- **Not en route** — a covered trip still not marked On The Way near pickup.
- **Not arrived** — a covered trip still not marked Arrived after pickup.

Thresholds come from the `DispatchAlertConfig` singleton (Settings screen). New and
escalated exceptions are pushed to the notification tray, an email digest, and — for the
critical tier — SMS. A milestone that has since been met resolves its exception silently.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.leads.models import Lead
from apps.notifications.email import send_html_email
from apps.notifications.models import Notification
from apps.reservations.models import Reservation

from .models import DispatchAlertConfig, DispatchException
from .selectors import CANCELLED_STATUSES, COVERAGE_CONFIRMED

log = logging.getLogger(__name__)

TS = Reservation.TripStatus
EN_ROUTE_OR_BEYOND = frozenset(
    {TS.ON_THE_WAY, TS.CIRCLING, TS.ARRIVED, TS.CUSTOMER_IN_CAR, TS.DONE}
)
ARRIVED_OR_BEYOND = frozenset({TS.ARRIVED, TS.CUSTOMER_IN_CAR, TS.DONE})

_TIER_RANK = {DispatchException.Tier.WARNING: 1, DispatchException.Tier.CRITICAL: 2}
K = DispatchException.Kind
T = DispatchException.Tier


def _has_confirmed_coverage(trip: Reservation) -> bool:
    # `active_list` is set by the board prefetch; fall back to a query off the board.
    rows = getattr(trip, "active_list", None)
    if rows is not None:
        return any(a.status == COVERAGE_CONFIRMED for a in rows)
    return trip.assignments.filter(status=COVERAGE_CONFIRMED).exists()


def evaluate(trip: Reservation, cfg: DispatchAlertConfig, now) -> dict[str, str]:
    """The milestones this trip is currently failing → the tier of each."""
    pickup = trip.pickup_at
    if pickup is None:
        return {}
    minutes_to = (pickup - now).total_seconds() / 60
    hours_to = minutes_to / 60
    covered = _has_confirmed_coverage(trip)
    status = trip.trip_status
    out: dict[str, str] = {}

    if not covered:
        if hours_to <= cfg.unassigned_critical_hours:
            out[K.UNASSIGNED] = T.CRITICAL
        elif hours_to <= cfg.unassigned_warn_hours:
            out[K.UNASSIGNED] = T.WARNING

    # The en-route / arrived milestones only make sense once someone is covering the trip —
    # an uncovered trip's real problem is the one above.
    if covered and status not in EN_ROUTE_OR_BEYOND:
        if minutes_to <= cfg.otw_critical_minutes:
            out[K.NOT_ON_THE_WAY] = T.CRITICAL
        elif minutes_to <= cfg.otw_warn_minutes:
            out[K.NOT_ON_THE_WAY] = T.WARNING

    if covered and status not in ARRIVED_OR_BEYOND:
        minutes_past = -minutes_to
        if minutes_past >= cfg.arrived_critical_minutes:
            out[K.NOT_ARRIVED] = T.CRITICAL
        elif minutes_past >= cfg.arrived_warn_minutes:
            out[K.NOT_ARRIVED] = T.WARNING

    return out


def _monitored_trips(cfg: DispatchAlertConfig, now) -> list[Reservation]:
    """Booked, uncancelled trips whose pickup is close enough that a milestone could bite.

    The window is derived from the widest threshold so raising `unassigned_warn_hours` to
    days-out still works.
    """
    lookahead = timedelta(hours=cfg.unassigned_warn_hours) + timedelta(days=1)
    lookback = timedelta(minutes=cfg.arrived_critical_minutes) + timedelta(days=1)
    return list(
        Reservation.objects.filter(
            lead__status=Lead.Status.BOOKED,
            pickup_date__range=((now - lookback).date(), (now + lookahead).date()),
        )
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .select_related("lead", "lead__contact")
        .prefetch_related("assignments", "dispatch_exceptions")
    )


def _record(trip: Reservation, kind: str, tier: str, now) -> tuple[DispatchException, bool]:
    """Open the exception, or escalate / reopen an existing one. Returns (row, notify?)."""
    exc, created = DispatchException.objects.get_or_create(
        reservation=trip, kind=kind, defaults={"tier": tier, "opened_at": now}
    )
    reopened = False
    if not created and exc.resolved_at is not None:
        exc.resolved_at = None
        exc.opened_at = now
        reopened = True
    if not created:
        exc.tier = tier
        exc.save(update_fields=["tier", "resolved_at", "opened_at", "updated_at"])
    return exc, (created or reopened or exc.notified_tier != tier)


def _resolve_cleared(trip: Reservation, still_open: set[str], now) -> None:
    for exc in trip.dispatch_exceptions.all():
        if exc.resolved_at is None and exc.kind not in still_open:
            exc.resolved_at = now
            exc.save(update_fields=["resolved_at", "updated_at"])


def run_dispatch_monitor() -> int:
    """One monitor pass. Returns how many exceptions were newly raised or escalated."""
    cfg = DispatchAlertConfig.load()
    if not cfg.enabled:
        return 0
    now = timezone.now()

    to_notify: list[DispatchException] = []
    for trip in _monitored_trips(cfg, now):
        breaches = evaluate(trip, cfg, now)
        for kind, tier in breaches.items():
            exc, notify = _record(trip, kind, tier, now)
            if notify:
                to_notify.append(exc)
        _resolve_cleared(trip, set(breaches), now)

    if to_notify:
        _raise_alerts(cfg, to_notify, now)
    return len(to_notify)


# --- alerting -------------------------------------------------------------------------


def _line(exc: DispatchException) -> dict:
    trip = exc.reservation
    return {
        "quote_no": trip.lead.quote_no,
        "customer": trip.lead.contact.name,
        "kind": exc.get_kind_display(),
        "tier": exc.tier,
        "pickup": trip.pickup_at,
        "tz": trip.pickup_tz_abbrev,
        "trip_id": trip.pk,
    }


def _raise_alerts(cfg: DispatchAlertConfig, excs: list[DispatchException], now) -> None:
    critical = [e for e in excs if e.tier == T.CRITICAL]

    # (a) notification tray — one row per exception, on its lead
    for exc in excs:
        Notification.notify(
            exc.reservation.lead,
            Notification.Kind.DISPATCH_EXCEPTION,
            title=f"{exc.get_tier_display()}: {exc.get_kind_display()}",
            detail=(
                f"Trip #{exc.reservation_id} · {exc.reservation.lead.contact.name} · "
                f"pickup {exc.reservation.pickup_at:%b %-d %-I:%M %p}"
                if exc.reservation.pickup_at
                else f"Trip #{exc.reservation_id}"
            ),
        )

    # (b) email digest
    lines = [_line(e) for e in excs]
    n = len(excs)
    subject = f"{n} dispatch exception{'s' if n != 1 else ''} need{'' if n != 1 else 's'} attention"
    if critical:
        subject = f"CRITICAL — {subject}"
    context = {
        "lines": lines,
        "critical_count": len(critical),
        "now": now,
        "company_name": settings.COMPANY_NAME,
        "board_url": f"{settings.PUBLIC_BASE_URL}/portal/dispatch/"
        if settings.PUBLIC_BASE_URL
        else "",
    }
    for recipient in cfg.email_list:
        send_html_email(
            to=recipient, subject=subject, template="dispatch_exceptions", context=context
        )

    # (c) SMS — critical tier only
    if critical and cfg.sms_list:
        _text_critical(cfg.sms_list, critical)

    stamp = timezone.now()
    for exc in excs:
        exc.notified_tier = exc.tier
        exc.save(update_fields=["notified_tier", "updated_at"])
    log.info("dispatch monitor: %d exception(s) alerted at %s", len(excs), stamp)


def _text_critical(numbers: list[str], critical: list[DispatchException]) -> None:
    from apps.integrations import podium

    body = f"APC dispatch: {len(critical)} CRITICAL exception(s)."
    for exc in critical[:5]:
        who = exc.reservation.lead.contact.name
        body += f"\n• #{exc.reservation_id} {exc.get_kind_display()} — {who}"
    for number in numbers:
        try:
            podium.send_message(identifier=number, body=body, channel_type="phone")
        except Exception:
            log.exception("dispatch monitor: SMS to %s failed", number)
