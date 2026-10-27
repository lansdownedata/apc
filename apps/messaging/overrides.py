"""Resolve a message: the owner's `MessageTemplate` row layered over the code default."""

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from . import fields
from .models import MessageTemplate, NotificationConfig, TouchPoint
from .touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING, TouchPointTemplate

TIMING_UNITS = ("minutes", "hours", "days")
_UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400}
MAX_TIMING = {"minutes": 1440, "hours": 720, "days": 60}


def default(kind: str) -> TouchPointTemplate:
    return THANK_YOU_WEDDING if kind == "thank_you_wedding" else TEMPLATES[kind]


def load() -> dict[str, MessageTemplate]:
    """Every override row, keyed by kind, in one query."""
    return {row.kind: row for row in MessageTemplate.objects.all()}


def _row_offset(row, base: TouchPointTemplate) -> timedelta | None:
    if (
        base.anchor == "triggered"
        or not row
        or row.timing_amount is None
        or row.timing_unit not in TIMING_UNITS
    ):
        return None
    span = timedelta(**{row.timing_unit: row.timing_amount})
    return -span if base.offset < timedelta(0) else span


def effective(kind: str, rows: dict | None = None) -> TouchPointTemplate:
    base = default(kind)
    rows = load() if rows is None else rows
    row = rows.get(kind)
    if row is None:
        return base
    changes = {f: getattr(row, f) for f in ("subject", "email_body", "sms_body") if getattr(row, f)}
    offset = _row_offset(row, base)
    if offset is not None:
        changes["offset"] = offset
    return replace(base, **changes)


def timing_of(kind: str, rows: dict | None = None) -> tuple[int, str] | None:
    """The effective (amount, unit) for a kind that has a timing, else None."""
    if default(kind).anchor == "triggered":
        return None
    seconds = abs(int(effective(kind, rows).offset.total_seconds()))
    for unit in reversed(TIMING_UNITS):
        if seconds and seconds % _UNIT_SECONDS[unit] == 0:
            return seconds // _UNIT_SECONDS[unit], unit
    return seconds // 60, "minutes"


# Off until the owner turns them on: the client may not want all three live day one.
DEFAULT_OFF = frozenset({"status_on_the_way", "status_arrived", "status_dispatched"})

# Kinds that have no switch of their own and follow another kind's.
_SWITCH_OF = {"trip_confirm_customer_2": "trip_confirm_customer", "thank_you_wedding": "thank_you"}


def is_enabled(kind: str, rows: dict | None = None, *, master: bool | None = None) -> bool:
    """Whether a message may go out: the master switch, then the kind's row, then the default.

    ``master`` lets a batch read the `NotificationConfig` singleton once.
    """
    if master is None:
        master = NotificationConfig.load().enabled
    if not master:
        return False
    switch = _SWITCH_OF.get(kind, kind)
    rows = load() if rows is None else rows
    row = rows.get(switch)
    if row is not None:
        return row.enabled
    return switch not in DEFAULT_OFF


class SaveError(Exception):
    """A draft that failed validation; `.errors` maps a box (or "timing") to its message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__(errors)
        self.errors = errors


def check(
    kind: str,
    *,
    subject: str,
    email_body: str,
    sms_body: str,
    timing: tuple[int, str] | None,
) -> dict[str, str]:
    """Per-box errors for a draft plus "timing"; {} when valid."""
    errors = fields.validate(kind, subject=subject, email_body=email_body, sms_body=sms_body)
    if timing is not None:
        amount, unit = timing
        if default(kind).anchor == "triggered":
            errors["timing"] = "This message sends when it happens; it has no timing."
        elif unit not in TIMING_UNITS:
            errors["timing"] = "Pick minutes, hours or days."
        elif not 0 <= amount <= MAX_TIMING[unit]:
            errors["timing"] = f"Use 0 to {MAX_TIMING[unit]} {unit}."
    return errors


def _offset_of(kind: str) -> timedelta:
    return effective(kind).offset


def _signed(base: TouchPointTemplate, timing: tuple[int, str]) -> timedelta:
    span = timedelta(**{timing[1]: timing[0]})
    return -span if base.offset < timedelta(0) else span


def save(
    kind: str,
    *,
    subject: str,
    email_body: str,
    sms_body: str,
    timing: tuple[int, str] | None,
    enabled: bool,
    user,
) -> int:
    """Store a message's differences from its default; returns how many sends were re-dated."""
    errors = check(kind, subject=subject, email_body=email_body, sms_body=sms_body, timing=timing)
    if errors:
        raise SaveError(errors)
    base = default(kind)
    texts = {
        "subject": "" if subject == base.subject else subject,
        "email_body": "" if email_body == base.email_body else email_body,
        "sms_body": "" if sms_body == base.sms_body else sms_body,
    }
    amount, unit = timing if timing is not None else (None, "")
    if timing is not None and _signed(base, timing) == base.offset:
        amount, unit = None, ""
    default_enabled = _SWITCH_OF.get(kind, kind) not in DEFAULT_OFF
    with transaction.atomic():
        old = _offset_of(kind)
        if not any(texts.values()) and amount is None and enabled == default_enabled:
            MessageTemplate.objects.filter(kind=kind).delete()
        else:
            MessageTemplate.objects.update_or_create(
                kind=kind,
                defaults={
                    **texts,
                    "timing_amount": amount,
                    "timing_unit": unit,
                    "enabled": enabled,
                    "updated_by": user,
                },
            )
        new = _offset_of(kind)
        return redate(kind, from_offset=old) if new != old else 0


def reset(kind: str) -> int:
    """Drop the owner's row for a kind; re-dates scheduled sends if its timing changed."""
    with transaction.atomic():
        old = _offset_of(kind)
        MessageTemplate.objects.filter(kind=kind).delete()
        new = _offset_of(kind)
        return redate(kind, from_offset=old) if new != old else 0


_RESERVATION_KINDS = frozenset(
    {
        "wed_final_details",
        "trip_confirm_customer",
        "trip_confirm_customer_2",
        "trip_confirm_affiliate",
        "driver_released",
    }
)


def redate(kind: str, *, from_offset: timedelta) -> int:
    """Re-date every SCHEDULED send of a kind to its anchor plus the effective offset.

    Reservation-anchored kinds are recomputed from the trip's pickup (rows may have been
    scheduled "now", so their date is not anchor + offset); driver_released never lands in
    the past. Lead-level kinds recover the anchor as `scheduled_for - from_offset`, the
    offset they were scheduled with. Returns the count."""
    if default(kind).anchor == "triggered":
        return 0
    new = _offset_of(kind)
    qs = TouchPoint.objects.filter(
        kind=kind, status=TouchPoint.Status.SCHEDULED, scheduled_for__isnull=False
    )
    rows = []
    if kind in _RESERVATION_KINDS:
        now = timezone.now()
        for row in qs.select_related("reservation"):
            pickup = row.reservation.pickup_at if row.reservation else None
            if pickup is None:
                continue
            at = pickup + new
            row.scheduled_for = max(now, at) if kind == "driver_released" else at
            rows.append(row)
    else:
        for row in qs:
            row.scheduled_for = row.scheduled_for - from_offset + new
            rows.append(row)
    TouchPoint.objects.bulk_update(rows, ["scheduled_for"])
    return len(rows)


_ANCHOR_PHRASE = {
    "lead_created": "Lead came in",
    "quote_sent": "Quote sent",
    "quote_viewed": "Quote viewed",
    "quote_expires": "Quote expires",
    "trips_done": "Trips done",
    "balance_due": "Balance due",
}
_PICKUP_ANCHORS = ("reservation_pickup", "dispatched_pickup")
_SATURDAY_ANCHORS = ("quote_expires", "trips_done", "balance_due")


def timing_example(kind: str, amount: int, unit: str, *, now: datetime | None = None) -> str:
    """A worked example of a timing, e.g. "Quote sent Mon Nov 9, 9:00 AM EST → sends ..."."""
    from apps.tasks import schedule

    base = default(kind)
    if base.anchor == "triggered":
        raise ValueError(f"{kind} has no timing")
    if unit not in TIMING_UNITS or not 0 <= amount <= MAX_TIMING[unit]:
        raise ValueError(f"bad timing {amount} {unit}")
    pickup = schedule.sample_pickup(now)
    if base.anchor in _PICKUP_ANCHORS or base.anchor in _SATURDAY_ANCHORS:
        anchor = pickup
    else:
        zone = timezone.get_current_timezone()
        anchor = datetime.combine(pickup.date() - timedelta(days=5), time(9, 0), tzinfo=zone)
    sends = anchor + _signed(base, (amount, unit))
    phrase = (
        f"{schedule.stamp(anchor)} pickup"
        if base.anchor in _PICKUP_ANCHORS
        else f"{_ANCHOR_PHRASE[base.anchor]} {schedule.stamp(anchor)}"
    )
    return f"{phrase} → sends {schedule.stamp(sends.astimezone(anchor.tzinfo))}"


@dataclass(frozen=True)
class MessageSpec:
    kind: str
    label: str
    group: str


MESSAGES: tuple[MessageSpec, ...] = tuple(
    MessageSpec(kind, label, group)
    for group, items in (
        (
            "Leads & quotes",
            (
                ("tp1_welcome", "Welcome message"),
                ("tp2_lead_followup", "Lead follow-up"),
                ("tp3_quote_sent_sms", "Quote sent"),
                ("tp4_viewed_sms", "Quote viewed (text)"),
                ("tp5_viewed_email", "Quote viewed (email)"),
                ("tp6_quote_followup", "Quote follow-up"),
                ("tp7_expiring", "Quote expiring"),
                ("tp8_expired", "Quote expired"),
            ),
        ),
        (
            "Orders",
            (
                ("order_confirmed", "Order confirmed"),
                ("order_cancelled", "Order cancelled"),
                ("order_auth_expired", "Deposit hold released"),
                ("payment_reminder", "Payment reminder"),
            ),
        ),
        (
            "Before the trip",
            (
                ("wed_final_details", "Wedding details request"),
                ("trip_confirm_customer", "Trip confirmation"),
                ("trip_confirm_affiliate", "Affiliate trip confirmation"),
            ),
        ),
        (
            "Trip day",
            (
                ("driver_released", "Driver details"),
                ("status_on_the_way", "On the way"),
                ("status_arrived", "Arrived"),
            ),
        ),
        (
            "After the trip",
            (
                ("thank_you", "Thank-you"),
                ("thank_you_wedding", "Thank-you (wedding)"),
                ("review_request", "Review request"),
            ),
        ),
    )
    for kind, label in items
)

_TIMING_WHAT = {
    "lead_created": "the lead comes in",
    "quote_sent": "the quote is sent",
    "quote_viewed": "the quote is viewed",
    "quote_expires": "the quote expires",
    "trips_done": "the trips are done",
    "balance_due": "the balance is due",
}
_UNIT_WORD = {"minutes": ("min", "min"), "hours": ("hour", "hours"), "days": ("day", "days")}


def timing_phrase(kind: str, rows: dict | None = None) -> str:
    """The row's timing in words, e.g. "2 hours after the lead comes in"."""
    timing = timing_of(kind, rows)
    if timing is None:
        return "When it happens"
    amount, unit = timing
    word = _UNIT_WORD[unit][0 if amount == 1 else 1]
    base = default(kind)
    side = "before" if base.offset < timedelta(0) else "after"
    if base.anchor in _PICKUP_ANCHORS:
        what = "pickup" + (", once Dispatched" if base.anchor == "dispatched_pickup" else "")
    else:
        what = _TIMING_WHAT[base.anchor]
    return f"{amount} {word} {side} {what}"
