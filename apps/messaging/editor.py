"""The message editor's logic (spec C sections 2, 6 and 9): storing an edited row and what
that does to its queued sends, reset and archive, timing rules and the worked example, and
the preview.

`apps.settings.forms.MessageForm` validates a draft and puts it on the row; `save` stores it.
Nothing here sends: the preview renders with the real context and link builders and resolves
the real recipients, but never reaches a transport.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import defaults, events, recipients, rules, triggers
from . import fields as message_fields
from . import rows as message_rows
from .defaults import TIMING_UNITS
from .models import MessageTemplate, TouchPoint
from .touchpoint_templates import build_context, render

MAX_TIMING = {"minutes": 1440, "hours": 720, "days": 60}
MIN_DRIVER_LEAD = "The driver message must go out at least 1 hour before pickup."
NO_TIMING = "This message sends when it happens; it has no timing."
# Triggers whose rows have no timing: a staff click sends inside the click, a schedule on its
# cron.
_UNTIMED = ("staff_click", "schedule")


def has_timing(trigger_key: str) -> bool:
    trigger = triggers.CATALOG.get(trigger_key)
    return trigger is not None and trigger.kind not in _UNTIMED


def is_staff_click(trigger_key: str) -> bool:
    """Staff-click rows (button sends, invites, password resets) have no on/off switch."""
    trigger = triggers.CATALOG.get(trigger_key)
    return trigger is not None and trigger.kind == "staff_click"


def _builtin(key: str, trigger_key: str, seeded: bool) -> bool:
    default = defaults.DEFAULTS.get(key)
    return bool(seeded and default is not None and default.trigger == trigger_key)


def _seconds(timing: tuple[int, str]) -> int:
    return timing[0] * message_rows.UNIT_SECONDS[timing[1]]


def check_timing(
    key: str, trigger_key: str, timing: tuple[int, str] | None, *, seeded: bool = True
) -> dict[str, str]:
    """The timing's error as {"timing": message}, else {}. ``timing`` is (amount, unit), or
    None for none. A seeded message on its own trigger keeps its own rules: the driver
    message at least an hour before pickup, the second confirmation after the first."""
    if timing is None:
        return {}
    if not has_timing(trigger_key):
        return {"timing": NO_TIMING}
    amount, unit = timing
    builtin = _builtin(key, trigger_key, seeded)
    error = ""
    if unit not in TIMING_UNITS:
        error = "Pick minutes, hours or days."
    elif not 0 <= amount <= MAX_TIMING[unit]:
        error = f"Use 0 to {MAX_TIMING[unit]} {unit}."
    elif builtin and key == "driver_released" and timedelta(**{unit: amount}) < timedelta(hours=1):
        error = MIN_DRIVER_LEAD
    elif builtin and key == "trip_confirm_customer_2":
        first = message_rows.timing_of("trip_confirm_customer")
        if first and _seconds(timing) >= _seconds(first):
            error = "The second notice must go out after the first."
    elif builtin and key == "trip_confirm_customer":
        second = message_rows.timing_of("trip_confirm_customer_2")
        if second and _seconds(timing) <= _seconds(second):
            error = "The first notice must go out before the second."
    return {"timing": error} if error else {}


# --- storing ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Was:
    trigger: str
    offset: timedelta
    enabled: bool


def _lock(row: MessageTemplate) -> _Was | None:
    """The row as stored, locked for the save; None for a new row."""
    if row.pk is None:
        return None
    stored = MessageTemplate.objects.select_for_update().get(pk=row.pk)
    return _Was(stored.trigger, stored.offset, stored.enabled)


def _offset(amount: int, unit: str, direction: str) -> timedelta:
    span = timedelta(**{unit: amount})
    return -span if direction == MessageTemplate.Direction.BEFORE else span


def _normalise(row: MessageTemplate) -> None:
    """What the trigger dictates: no timing on staff clicks and schedules, "after" off a date,
    and a staff click is always on. A seeded row timed the same as its original keeps the
    original's units (72 hours stays 72 hours, not 3 days)."""
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is not None and trigger.kind in _UNTIMED:
        row.timing_amount, row.timing_unit = 0, "minutes"
    if trigger is not None and trigger.kind != "date":
        row.timing_direction = MessageTemplate.Direction.AFTER
    if is_staff_click(row.trigger):
        row.enabled = True
    default = defaults.DEFAULTS.get(row.key)
    if row.seeded and default is not None and has_timing(row.trigger):
        original = _offset(default.timing_amount, default.timing_unit, default.timing_direction)
        if row.offset == original:
            row.timing_amount, row.timing_unit = default.timing_amount, default.timing_unit


def save(row: MessageTemplate, *, user=None, service_types=None) -> int:
    """Store a row as edited, then bring its queued sends in line (`_follow`). A new row gets
    the key `custom-<pk>`. ``service_types`` (when given) replaces the row's. Returns how many
    queued sends were re-dated."""
    _normalise(row)
    row.updated_by = user
    with transaction.atomic():
        was = _lock(row)
        if row.pk is None:
            row.seeded = False
            row.key = f"new-{uuid4().hex}"[:40]
            row.save()
            row.key = f"custom-{row.pk}"
            row.save(update_fields=["key"])
        else:
            row.save()
        if service_types is not None:
            row.service_types.set(service_types)
    return _follow(row, was)


def reset(row: MessageTemplate, *, user=None) -> int:
    """Put a seeded row back to its original, switch included; returns how many queued sends
    were re-dated."""
    with transaction.atomic():
        was = _lock(row)
        defaults.reset(row)
        row.enabled = defaults.DEFAULTS[row.key].enabled
        row.updated_by = user
        row.save(update_fields=["enabled", "updated_by", "updated_at"])
    return _follow(row, was)


def set_enabled(row: MessageTemplate, enabled: bool, *, user=None) -> None:
    """Flip a row's switch; switching off withdraws its queued sends."""
    with transaction.atomic():
        was = _lock(row)
        row.enabled = enabled
        row.updated_by = user
        row.save(update_fields=["enabled", "updated_by", "updated_at"])
    _follow(row, was)


def archive(row: MessageTemplate, *, user=None) -> int:
    """Archive an added row: off, hidden from the page and `live()`, its queued sends
    withdrawn. The row itself stays (sent TouchPoints and Deliveries keep pointing at it).
    Returns how many queued sends were withdrawn."""
    with transaction.atomic():
        _lock(row)
        row.archived_at = timezone.now()
        row.enabled = False
        row.updated_by = user
        row.save(update_fields=["archived_at", "enabled", "updated_by", "updated_at"])
        return events.withdraw(row)


def _follow(row: MessageTemplate, was: _Was | None) -> int:
    """After a row change has committed (so a failure here cannot poison it): switched off
    withdraws its queued sends; a new trigger withdraws them and queues the row for the
    booked orders when it is a date; a new timing re-dates them. Returns the re-dated
    count, the Driver details checklist tasks included."""
    moved = was is None or row.trigger != was.trigger
    count = 0
    if was is not None:
        if not row.enabled or moved:
            events.withdraw(row)
        elif row.offset != was.offset:
            count = events.redate(row, old_offset=was.offset)
    if row.enabled and moved:
        _queue_dates(row)
    changed = was is not None and (moved or row.offset != was.offset)
    return count + _redate_checklist(row.key, changed=changed)


def _queue_dates(row: MessageTemplate) -> None:
    """A row now on a date trigger: queue it for every booked order with a live trip ahead,
    through `events.schedule_dates` (per trip for a trip date, per order otherwise); a row
    that waits for dispatch through `events.sync_waiting`."""
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead

    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is None or trigger.kind != "date":
        return
    now = timezone.now()
    leads = (
        Lead.objects.filter(
            status=Lead.Status.BOOKED,
            reservations__pickup_date__gte=timezone.localdate() - timedelta(days=1),
        )
        .distinct()
        .prefetch_related("reservations")
    )
    for lead in leads:
        trips = [
            r
            for r in lead.reservations.all()
            if r.trip_status not in CANCELLED_STATUSES and r.pickup_at and r.pickup_at > now
        ]
        if not trips:
            continue
        if trigger.level == "order":
            events.schedule_dates(lead)
            continue
        for trip in trips:
            if trigger.waits_for:
                events.sync_waiting(trip)
            else:
                events.schedule_dates(lead, trip)


def _redate_checklist(key: str, *, changed: bool) -> int:
    """The "Driver info released" checklist task is due with the driver message, so a new
    timing re-dates its open tasks too. Returns how many."""
    if key != "driver_released" or not changed:
        return 0
    from apps.tasks import schedule

    return schedule.redate({("driver_released", "standard")})


# --- describing the timing -------------------------------------------------------------

# How a worked example names the moment a trigger counts from.
_PHRASE = {
    "lead_created": "Lead came in",
    "quote_sent": "Quote sent",
    "quote_viewed": "Quote viewed",
    "quote_expiry": "Quote expires",
    "balance_due": "Balance due",
    "all_trips_done": "Trips done",
}
_ANCHOR_NOUN = {
    "pickup": "pickup",
    "pickup_dispatched": "pickup, once dispatched",
    "quote_expiry": "the quote expires",
    "balance_due": "the balance due date",
}


def anchor_noun(trigger_key: str) -> str:
    """What "before"/"after" counts from: "pickup", or "it happens" for an event."""
    return _ANCHOR_NOUN.get(trigger_key, "it happens")


def timing_example(
    trigger_key: str,
    amount: int,
    unit: str,
    direction: str = "after",
    *,
    now: datetime | None = None,
) -> str:
    """A worked example, e.g. "Quote sent Mon Nov 9, 9:00 AM EST → sends Mon Nov 9, 9:30 AM
    EST". Dates and trip events count from a sample Saturday pickup, order events from the
    Monday before. Raises ValueError for a trigger with no timing or a timing out of
    bounds."""
    from apps.tasks import schedule

    trigger = triggers.CATALOG.get(trigger_key)
    if trigger is None or not has_timing(trigger_key):
        raise ValueError(f"{trigger_key} has no timing")
    if unit not in TIMING_UNITS or not 0 <= amount <= MAX_TIMING[unit]:
        raise ValueError(f"bad timing {amount} {unit}")
    pickup = schedule.sample_pickup(now)
    if trigger.kind == "date" or trigger.level == "trip" or trigger_key == "all_trips_done":
        anchor = pickup
    else:
        zone = timezone.get_current_timezone()
        anchor = datetime.combine(pickup.date() - timedelta(days=5), time(9, 0), tzinfo=zone)
    before = trigger.kind == "date" and direction == MessageTemplate.Direction.BEFORE
    sends = anchor + _offset(amount, unit, "before" if before else "after")
    if trigger.anchor is not None and trigger_key not in _PHRASE:
        phrase = f"{schedule.stamp(anchor)} pickup"
    else:
        phrase = f"{_PHRASE.get(trigger_key, trigger.label)} {schedule.stamp(anchor)}"
    return f"{phrase} → sends {schedule.stamp(sends.astimezone(anchor.tzinfo))}"


def behaviour(trigger_key: str) -> list[str]:
    """How the trigger behaves, as the editor lists it under the timing."""
    trigger = triggers.CATALOG.get(trigger_key)
    if trigger is None:
        return []
    lines = []
    if trigger.waits_for:
        lines += [
            "Waits until the trip is Dispatched with a driver name and cell.",
            "If it is dispatched after this time, it sends right away while the pickup is "
            "still ahead.",
        ]
    if trigger.combine:
        lines.append("One message per person per day, listing all their trips that day.")
    elif trigger.kind == "event" and trigger.level == "trip":
        lines.append("One message per trip.")
    if trigger.kind == "schedule":
        lines.append("Sends on the report's own schedule; skipped when there is nothing in it.")
    return lines


# --- preview ---------------------------------------------------------------------------

_PREVIEW_SCAN = 200


def _live_trips(lead) -> list:
    from apps.dispatch.selectors import CANCELLED_STATUSES

    return [r for r in lead.reservations.all() if r.trip_status not in CANCELLED_STATUSES]


def _matching_trips(lead, service_types) -> list:
    types = list(service_types)
    return [t for t in _live_trips(lead) if not types or rules.trip_matches(t, types)]


def _has_driver_info(reservation) -> bool:
    assignment = rules.confirmed_assignment(reservation)
    return bool(assignment and assignment.driver_info)


def _wants_driver(trigger) -> bool:
    return "Driver" in trigger.field_groups


def preview_orders(trigger_key: str, service_types=(), limit: int = 20) -> list:
    """Booked orders to preview a message against, newest first.

    A trip-level trigger needs a live trip, and chosen service types a live trip of one of
    them; a trigger with driver fields lists orders with driver details on file first."""
    from apps.leads.models import Lead

    trigger = triggers.get(trigger_key)
    types = list(service_types)
    candidates = (
        Lead.objects.filter(status=Lead.Status.BOOKED)
        .select_related("contact")
        .prefetch_related(
            "reservations__stops",
            "reservations__vehicle",
            "reservations__service_type",
            "reservations__assignments__driver",
            "reservations__assignments__vendor",
        )
        .order_by("-created_at", "-pk")[:_PREVIEW_SCAN]
    )
    leads = []
    for lead in candidates:
        if (trigger.level == "trip" or types) and not _matching_trips(lead, types):
            continue
        leads.append(lead)
    if _wants_driver(trigger):
        leads.sort(key=lambda lead: not any(_has_driver_info(r) for r in _live_trips(lead)))
    return leads[:limit]


def preview_trip(trigger_key: str, lead, service_types=()):
    """The trip of an order a trip-level message is previewed against, else None."""
    trigger = triggers.get(trigger_key)
    if trigger.level != "trip":
        return None
    trips = _matching_trips(lead, service_types)
    if _wants_driver(trigger):
        trips = sorted(trips, key=lambda r: not _has_driver_info(r))
    return trips[0] if trips else None


def sample_context(base_url: str) -> dict[str, str]:
    """Plausible values for every field, for previewing without an order."""
    from apps.tasks import schedule

    pickup = schedule.sample_pickup()
    date_text = f"{pickup:%b %d, %Y}"
    time_text = f"{pickup:%-I:%M %p} {pickup:%Z}"
    ctx = dict.fromkeys(message_fields.FIELDS, "")
    ctx.update(
        {
            "first_name": "Sample",
            "quote_no": "Q-1042",
            "pickup_date": date_text,
            "pickup_time": time_text,
            "pickup_address": "1 Main St, Washington, DC",
            "dropoff_address": "Dulles International Airport (IAD)",
            "quote_breakdown": (
                f"{pickup:%b %d} · {time_text} — Luxury SUV · 4 passengers — $450.00"
            ),
            "agent_first": "Alex",
            "balance_amount": "$225.00",
            "balance_due_date": f"{pickup - timedelta(days=30):%b %d, %Y}",
            "card_last4": "4242",
            "trip_pickup_date": date_text,
            "trip_pickup_time": time_text,
            "trip_pickup_tz": f"{pickup:%Z}",
            "trip_pickup_when": f"{date_text} at {time_text}",
            "trip_routing": "1 Main St, Washington, DC → Dulles International Airport (IAD)",
            "trip_passengers": "4",
            "trip_passenger_names": "Dana Whitfield, Sam Lee",
            "trip_vehicle": "Luxury SUV",
            "trip_sheet": (
                f"Trip Q-1042\n{date_text} at {time_text}\n"
                "1 Main St, Washington, DC → Dulles International Airport (IAD)"
            ),
            "contact_on_file": "Sample Customer · (555) 010-0100",
            "driver_name": "Sam Driver",
            "driver_cell": "(555) 010-0199",
            "vehicle_description": "Black Cadillac Escalade",
            "vehicle_number": "12",
            "vendor_name": "Sample Limousines",
            "payout": "$300.00",
            "quote_link": f"{base_url}/quote/sample/",
            "pay_link": f"{base_url}/quote/sample/pay/",
            "confirm_link": f"{base_url}/confirm/sample/",
            "ack_link": f"{base_url}/acknowledge/sample/",
            "review_link": f"{base_url}/review/sample/",
            "company_name": settings.COMPANY_NAME,
            "company_phone": settings.COMPANY_PHONE,
            "company_email": settings.COMPANY_EMAIL,
        }
    )
    return ctx


_WORDS = {("sms",): "text", ("email",): "email", ("sms", "email"): "text and email"}
_MISSING = {"sms": "no mobile on file", "email": "no email on file"}


def _label(row: MessageTemplate, person: recipients.Recipient) -> str:
    return person.name if row.audience == MessageTemplate.Audience.STAFF else person.label


def _claimed(people: list, channel: str) -> set[int]:
    """Who sends on a channel: one per distinct address, the first to claim it."""
    reachable = [p for p in people if channel in p.channels]
    return {id(p) for p, _ in recipients.addresses(reachable, channel)}


def _to_lines(row: MessageTemplate, people: list) -> list[str]:
    """ "On this order it would go to", one "<label> · text|email|text and email" per person;
    someone it won't reach says why."""
    on = row.channels
    claimed = {c: _claimed(people, c) for c in on}
    lines = []
    for person in people:
        got = tuple(c for c in on if id(person) in claimed[c])
        if got:
            lines.append(f"{_label(row, person)} · {_WORDS[got]}")
            continue
        usable = [c for c in on if c in person.channels]
        has_address = any((person.phone if c == "sms" else person.email.strip()) for c in usable)
        if not usable:
            why = "texts only" if person.channels == ("sms",) else "email only"
        elif has_address:
            why = "same address as above, one copy"
        else:
            why = " and ".join(_MISSING[c] for c in usable)
        lines.append(f"{_label(row, person)} · skipped, {why}")
    return lines


def _sample_lines(row: MessageTemplate) -> list[str]:
    on = row.channels
    if row.audience == MessageTemplate.Audience.STAFF:
        return [f"{email} · email" for email in row.staff_emails or [] if "email" in on]
    lines = []
    for key in row.recipients or []:
        got = tuple(c for c in on if c == "sms" or key not in recipients.TEXT_ONLY)
        if got:
            lines.append(f"{recipients.LABELS.get(key, key)} · {_WORDS[got]}")
    return lines


def _render(text: str, ctx: dict) -> str:
    return render(text, ctx) if text.strip() else ""


def preview(row: MessageTemplate, lead, *, service_types=()) -> dict:
    """Render a draft row the way a send would, against an order or a built-in sample, with
    the people it would reach. Never sends.

    Returns {"sms", "email", "sms_to", "email_to", "to"}: each channel's text greeting its
    first recipient (blank when the channel is off), whom it is addressed to, and the
    "<label> · text|email" lines. The email is the subject line, a blank line and the body.
    """
    base_url = settings.PUBLIC_BASE_URL or "https://example.com"
    on = row.channels
    if lead is None:
        ctx = sample_context(base_url)
        contexts = {c: ctx for c in on}
        heads = {c: "" for c in on}
        lines = _sample_lines(row)
    else:
        trip = preview_trip(row.trigger, lead, service_types)
        tp = TouchPoint(lead=lead, reservation=trip, kind=row.key)
        people = rules.recipients_for(tp, row, recipients.resolve(row, lead, trip))
        lines = _to_lines(row, people)
        contexts, heads = {}, {}
        for channel in on:
            claimed = _claimed(people, channel)
            first = next((p for p in people if id(p) in claimed), None)
            ctx = build_context(lead, trip)
            if first is not None and row.audience in (
                MessageTemplate.Audience.CUSTOMER,
                MessageTemplate.Audience.PLANNER,
            ):
                ctx["first_name"] = (first.name or "").split(" ")[0] or "there"
            rules.fill_links(tp, row, ctx, base_url)
            contexts[channel] = ctx
            heads[channel] = _label(row, first) if first is not None else ""
    email = ""
    if "email" in contexts:
        head = _render(row.subject, contexts["email"])
        body = _render(row.email_body, contexts["email"])
        email = f"{head}\n\n{body}" if head and body else head or body
    sms = _render(row.sms_body, contexts["sms"]) if "sms" in contexts else ""
    return {
        "sms": sms,
        "email": email,
        "sms_to": heads.get("sms", ""),
        "email_to": heads.get("email", ""),
        "to": lines,
    }
