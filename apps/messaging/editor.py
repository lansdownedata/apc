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
from .models import MessageTemplate, NotificationConfig, TouchPoint
from .touchpoint_templates import build_context, render

MAX_TIMING = {"minutes": 1440, "hours": 720, "days": 60}
MIN_DRIVER_LEAD = "The driver message must go out at least 1 hour before pickup."
NO_TIMING = "This message sends when it happens; it has no timing."
# Triggers whose rows have no timing: a staff click sends inside the click, a schedule on its
# cron.
_UNTIMED = ("staff_click", "schedule")


def has_timing(trigger_key: str) -> bool:
    trigger = triggers.CATALOG.get(trigger_key)
    return trigger is not None and trigger.kind not in _UNTIMED and not trigger.caller_sends


def is_staff_click(trigger_key: str) -> bool:
    """Staff-click rows (button sends, invites, password resets) have no on/off switch."""
    trigger = triggers.CATALOG.get(trigger_key)
    return trigger is not None and trigger.kind == "staff_click"


# A seeded staff-click row's button decides on which channel and when it goes; the row
# holds the wording and, since APC-129, who gets it. Its trigger, audience, channels and
# service types are fixed, and the editor says this instead. An invite's recipient is the
# account holder its caller names, so those rows lock their recipients too.
LOCKED = ("trigger", "audience", "send_sms", "send_email")
INVITE_LOCKED = (*LOCKED, "recipients", "staff_emails")
SET_BY_BUTTON = "Set by the button; it can't be changed here."
CLICK_WHO = {
    "send_quote": (
        "Sent by text and email, as staff choose when they click Send quote, to the people "
        "ticked here."
    ),
    "send_quote_on_account": (
        "Sent by text and email, as staff choose when they click Send quote on an order "
        "billed to an account, to the people ticked here."
    ),
    "send_pay_link": (
        "Sent when staff click Send payment link: by text to anyone ticked here with a "
        "mobile, by email to the rest."
    ),
    "send_terms_link": (
        "Sent when staff click Send terms link: by text to anyone ticked here with a "
        "mobile, by email to the rest."
    ),
    "payment_receipt": (
        "Sent by email once a payment or refund is recorded, to the people ticked here; "
        "the booking contact stands in for a billing contact with no email."
    ),
    "affiliate_offer": (
        "Sent by email when staff offer a trip; Company means the affiliate being offered it."
    ),
    "affiliate_cancel": (
        "Sent by email when staff cancel a trip with an affiliate; Company means the "
        "affiliate the trip is taken back from."
    ),
}
# The invites, the reset and the wedding-details email: their caller names the person.
INVITE_WHO = {
    "planner_invite": (
        "Goes to the planner by email when staff click Invite or Resend — set by the button."
    ),
    "planner_password_reset": (
        "Goes to the planner who asked for a new password, by email — set by the button "
        "on the sign-in page."
    ),
    "user_invite": (
        "Goes to the new user by email when an admin clicks Invite or Resend — set by the button."
    ),
    "wedding_details_received": (
        "Goes to the person who sent the wedding details form, by email, the moment they "
        "send it — set by the form."
    ),
}
# Who every locked row goes to, by key.
WHO = {**CLICK_WHO, **INVITE_WHO}
# Nothing sends any other row on these: a staff click is sent by its button, a schedule by
# its job, each by its own seeded key.
_NOT_ADDABLE = ("staff_click", "schedule")
NOT_ADDABLE = "Staff clicks and schedules are sent by the app's own messages. Pick another trigger."


# A seeded staff report: its job decides when it goes, to staff, by email, for no order (so
# no service types); the row holds the wording around the table and who gets it.
REPORT_LOCKED = ("trigger", "audience", "send_sms", "send_email")
SET_BY_REPORT = "Set by the report; it can't be changed here."


def is_locked(row: MessageTemplate) -> bool:
    """A seeded staff-click, invite or wedding-details row: its button (or form), not the
    row, sets how and when."""
    return bool(row.seeded and (row.key in defaults.CLICKS or row.key in defaults.INVITES))


def recipients_locked(row: MessageTemplate) -> bool:
    """A seeded invite, reset or wedding-details row: its caller names the person, so the
    row lists no one and the editor offers no one to tick."""
    return bool(row.seeded and row.key in defaults.INVITES)


def is_report(row: MessageTemplate) -> bool:
    """A seeded staff report: its job sets how and when; who gets it is the row's."""
    return bool(row.seeded and row.key in defaults.REPORTS)


def locked_fields(row: MessageTemplate) -> tuple[tuple[str, ...], str]:
    """The fields a seeded row's sender fixes (service types aside: any locked row keeps
    them) and why, or ((), "") for a row the editor fully owns."""
    if recipients_locked(row):
        return INVITE_LOCKED, SET_BY_BUTTON
    if is_locked(row):
        return LOCKED, SET_BY_BUTTON
    if is_report(row):
        return REPORT_LOCKED, SET_BY_REPORT
    return (), ""


def can_pick(row: MessageTemplate, trigger_key: str) -> bool:
    """Whether ``row`` may be put on ``trigger_key``. A seeded row created on a staff click
    or a schedule keeps that trigger; no other row, seeded or added, moves onto one, since
    only the app's own sends use them (APC-129 tracks button sends that read their rows)."""
    trigger = triggers.CATALOG.get(trigger_key)
    if trigger is None:
        return False
    original = defaults.ORIGINALS.get(row.key) if row.seeded else None
    if original is not None and _own_sends_only(triggers.get(original.trigger)):
        return trigger_key == original.trigger
    return not _own_sends_only(trigger)


def _own_sends_only(trigger: triggers.Trigger) -> bool:
    """A trigger only the app's own seeded message is sent on: a staff click, a schedule, or
    an event its service sends itself (the wedding-details email)."""
    return trigger.kind in _NOT_ADDABLE or trigger.caller_sends


def _builtin(key: str, trigger_key: str, seeded: bool) -> bool:
    default = defaults.ORIGINALS.get(key)
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
    service_types: frozenset[int]


def _type_ids(row: MessageTemplate) -> frozenset[int]:
    return frozenset(row.service_types.values_list("pk", flat=True))


def _lock(row: MessageTemplate) -> _Was | None:
    """The row as stored, locked for the save; None for a new row."""
    if row.pk is None:
        return None
    stored = MessageTemplate.objects.select_for_update().get(pk=row.pk)
    return _Was(stored.trigger, stored.offset, stored.enabled, _type_ids(stored))


def _offset(amount: int, unit: str, direction: str) -> timedelta:
    span = timedelta(**{unit: amount})
    return -span if direction == MessageTemplate.Direction.BEFORE else span


def _normalise(row: MessageTemplate) -> None:
    """What the trigger dictates: no timing on staff clicks and schedules, "after" off a date,
    and a staff click is always on. A seeded row timed the same as its original keeps the
    original's units (72 hours stays 72 hours, not 3 days)."""
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is not None and not has_timing(row.trigger):
        row.timing_amount, row.timing_unit = 0, "minutes"
    if trigger is not None and trigger.kind != "date":
        row.timing_direction = MessageTemplate.Direction.AFTER
    if is_staff_click(row.trigger):
        row.enabled = True
    default = defaults.ORIGINALS.get(row.key)
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
        row.enabled = defaults.ORIGINALS[row.key].enabled
        row.updated_by = user
        row.save(update_fields=["enabled", "updated_by", "updated_at"])
    return _follow(row, was)


def set_enabled(row: MessageTemplate, enabled: bool | None, *, user=None) -> None:
    """Set a row's switch; switching off withdraws its queued sends. ``enabled`` None flips
    the switch as stored, read under the row lock, so two tabs flipping at once don't both
    act on the same stale value."""
    with transaction.atomic():
        was = _lock(row)
        row.enabled = (not was.enabled) if enabled is None else enabled
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
    withdraws its queued sends; a new trigger withdraws them and queues the row on a date
    trigger for the booked orders; switched back on queues a date row again (an event row's
    withdrawn sends stay withdrawn: the event has happened); new service types queue a date
    row for the orders it now matches (the sender re-checks the types of those already
    queued); a new timing re-dates them. Only this row's sends are touched. Returns the
    re-dated count, the Driver details checklist tasks included."""
    moved = was is None or row.trigger != was.trigger
    switched_on = was is not None and row.enabled and not was.enabled
    retyped = was is not None and _type_ids(row) != was.service_types
    count = 0
    if was is not None:
        if not row.enabled:
            events.withdraw(row, reason=events.SWITCHED_OFF)
        elif moved:
            events.withdraw(row)
        elif row.offset != was.offset:
            count = events.redate(row, old_offset=was.offset)
    if row.enabled and (moved or switched_on or retyped):
        _queue_dates(row)
    changed = was is not None and (moved or row.offset != was.offset)
    return count + _redate_checklist(row.key, changed=changed)


def _queue_dates(row: MessageTemplate) -> None:
    """Queue a row on a date trigger, and only that row, where its trigger can still send:
    a pickup date for every booked order with a live trip ahead (`events.schedule_dates`
    per trip, or `events.sync_waiting` for a row that waits for dispatch); a quote expiry
    for every open quote whose send time is still ahead; a balance due date for every
    order whose balance is scheduled on a card with the due date ahead. The master switch
    and the row are read once, not per order. Nothing is sent here: inside the Admin's
    request a send per order could outlast the worker, so what is already due waits for the
    cron (`run-touchpoints`), minutes away."""
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is None or trigger.kind != "date":
        return
    master = NotificationConfig.load().enabled
    if not master:
        return
    only = MessageTemplate.objects.prefetch_related("service_types").get(pk=row.pk)
    if trigger.level == "trip":
        for lead, trips in _booked_with_trips_ahead():
            for trip in trips:
                if trigger.waits_for:
                    events.sync_waiting(trip, only=only, master=master, send_inline=False)
                else:
                    events.schedule_dates(lead, trip, only=only, master=master, send_inline=False)
        return
    leads = _open_quotes(only) if trigger.key == "quote_expiry" else _balances_scheduled()
    for lead in leads:
        events.schedule_dates(lead, only=only, master=master, send_inline=False)


def _booked_with_trips_ahead():
    """(lead, its live trips with the pickup ahead) for every booked order that has any."""
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead

    now = timezone.now()
    leads = (
        Lead.objects.filter(
            status=Lead.Status.BOOKED,
            reservations__pickup_date__gte=timezone.localdate() - timedelta(days=1),
        )
        .distinct()
        .select_related("payment")
        .prefetch_related("reservations")
    )
    for lead in leads:
        trips = [
            r
            for r in lead.reservations.all()
            if r.trip_status not in CANCELLED_STATUSES and r.pickup_at and r.pickup_at > now
        ]
        if trips:
            yield lead, trips


def _open_quotes(row: MessageTemplate):
    """New or quoted orders whose expiry nudge from this row would still be ahead (the gate
    of `events._quote_expiry`; a long-lapsed quote is not nudged late)."""
    from apps.leads.models import Lead

    return Lead.objects.filter(
        status__in=(Lead.Status.NEW, Lead.Status.QUOTED),
        quote_expires_at__gt=timezone.now() - row.offset,
    ).select_related("payment")


def _balances_scheduled() -> list:
    """Orders whose balance is scheduled on a card with the due date ahead, whatever their
    status (the gate of `events._balance_due`, which also skips orders billed to an
    account)."""
    from apps.leads.models import Lead
    from apps.payments.models import PaymentPlan

    today = timezone.localdate()
    leads = (
        Lead.objects.filter(payment__balance_status=PaymentPlan.BalanceStatus.SCHEDULED)
        .exclude(payment__stripe_payment_method_id="")
        .select_related("payment")
        .prefetch_related("reservations")
    )
    return [
        lead
        for lead in leads
        if lead.payment.balance_due_date is not None and lead.payment.balance_due_date >= today
    ]


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


COMBINE_LINE = "One message per person per day, listing all their trips that day."


def behaviour(trigger_key: str, *, combines: bool = True) -> list[str]:
    """How the trigger behaves, as the editor lists it under the timing. ``combines`` is
    False for a row on a combining trigger that sends per trip (`rules.combines_per_day`),
    so the trigger's per-day line is left out."""
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
        if combines:
            lines.append(COMBINE_LINE)
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


def preview_orders(trigger_key: str, service_types=(), limit: int = 20, *, key: str = "") -> list:
    """Booked orders to preview a message against, newest first.

    An order is offered when the message would match it at send time (`rules.types_match`,
    as `rules.matches`): a trip-level trigger needs a live trip, chosen service types a
    live trip of one of them. ``key`` names the seeded message being previewed, so its own
    matching applies (the wedding thank-you also goes to an order with a wedding name). A
    trigger with driver fields lists orders with driver details on file first."""
    from apps.leads.models import Lead

    trigger = triggers.get(trigger_key)
    types = list(service_types)
    wedding_name = rules.wedding_name_counts(
        MessageTemplate(key=key, trigger=trigger_key, seeded=bool(key))
    )
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
        if trigger.level == "trip":
            offered = any(
                rules.types_match(types, lead, trip, trip_level=True) for trip in _live_trips(lead)
            )
        else:
            offered = rules.types_match(types, lead, trip_level=False, wedding_name=wedding_name)
        if offered:
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


# The page each account email's button opens, for its preview.
_ACCOUNT_PAGES = {
    "planner_invite": "/planner/register/sample/",
    "planner_password_reset": "/planner/password/sample/",
    "user_invite": "/invite/sample/",
}


def sample_context(base_url: str, trigger_key: str = "") -> dict[str, str]:
    """Plausible values for every field, for previewing without an order. An account
    email's button opens its own page (``trigger_key``)."""
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
            "trips_coming_up": f"Your trip on {date_text} at {time_text} is coming up",
            "trip_routing": "1 Main St, Washington, DC → Dulles International Airport (IAD)",
            "trip_passengers": "4",
            "trip_passenger_names": "Dana Whitfield, Sam Lee",
            "trip_vehicle": "Luxury SUV",
            "trip_sheet": (
                f"Trip Q-1042\n{date_text} at {time_text}\n"
                "1 Main St, Washington, DC → Dulles International Airport (IAD)"
            ),
            "trip_sheet_drivers": (
                f"Trip Q-1042\n{date_text} at {time_text}\n"
                "1 Main St, Washington, DC → Dulles International Airport (IAD)\n"
                "  Driver: Sam Driver · (555) 010-0199"
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
            # What a staff click's caller hands over (its own field groups).
            "contact_name": "Sample Customer",
            "quote_total": "$450.00",
            "deposit_pct": "50",
            "deposit_amount": "$225.00",
            "quote_valid_through": f"{pickup - timedelta(days=14):%B %-d, %Y}",
            "quote_summary": (
                "  QUOTE NO.       Q-1042\n  PREPARED FOR    Sample Customer\n"
                "  ESTIMATED TOTAL $450.00\n\nA 50% deposit of $225.00 reserves your booking."
            ),
            "button": f"View your quote & reserve:\n{base_url}/quote/sample/",
            "payment_for": "reservation Q-1042",
            "terms_link": f"{base_url}/quote/sample/terms/",
            "receipt_name": "receipt R-1042",
            "receipt_intro": f"We charged your card for your trip on {pickup:%A, %B %-d}.",
            "receipt_lines": (
                f"  AMOUNT CHARGED  $120.00\n  Visa ••4242 · {date_text} · Receipt R-1042\n\n"
                "  Overtime (1 hr): $120.00\n  Total: $120.00"
            ),
            "receipt_explanation": (
                "Your trip ran past its scheduled end, and time beyond the grace period "
                "bills by the hour, as in your booking terms."
            ),
            "receipt_kind": "charge",
            "trip_day": f"{pickup:%b %-d}",
            "cancelled_trip_sheet": (
                f"{pickup:%A, %B %-d, %Y} at {time_text}\nVehicle: Luxury SUV\nPassengers: 4\n\n"
                "Routing:\n  1. 1 Main St, Washington, DC\n"
                "  2. Dulles International Airport (IAD)"
            ),
            "account_name": "Sample Planner",
            "account_first_name": "Sample",
            "account_button": base_url
            + _ACCOUNT_PAGES.get(trigger_key, _ACCOUNT_PAGES["planner_invite"]),
            "role_label": "Agent",
            "form_name": "Sample Customer",
            "wedding_details": (
                f"  REFERENCE   Q-1042\n\n  DATE\n    Wedding date: {pickup:%A, %B %-d, %Y}\n\n"
                "  VENUE & CEREMONY\n    Reception venue: The Oak Barn at Loyalty"
            ),
            "resume_button": f"{base_url}/weddings/plan/sample/",
            # What a staff report's job hands over.
            "report_date": f"{pickup:%A, %B %-d, %Y}",
            "report_time": f"{pickup:%a %b %-d}, {time_text}",
            "period_start": f"{pickup - timedelta(days=30):%b %-d}",
            "period_end": f"{pickup:%b %-d, %Y}",
            "orders": "2 orders",
            "booked_orders": "2 booked orders",
            "window_days": "30",
            "customers": "2 customers",
            "trips": "3 trips",
            "trip_count": "3",
            "asked_count": "2",
            "held_count": "0",
            "reviewed_count": "1",
            "trip_page_ratings": "2 rated the trip on their trip page, averaging 4.5 out of 5.",
            "new_complaints": "1 new customer complaint",
            "overdue_tasks": "2 overdue tasks",
            "tasks": "2 tasks",
            "critical_mark": "CRITICAL — ",
            "alert_summary": "2 dispatch exceptions need attention",
            "exceptions": "2 exceptions",
            "critical_note": " (1 CRITICAL)",
            "report_table": (
                f"  Q-1042 · Sample Customer\n    1 trip · {date_text}\n"
                f"    {base_url}/portal/orders/1042/"
            ),
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
        if rules.text_first(row) and len(got) == 2:
            got = ("sms",)  # a link is texted to anyone with a mobile, emailed to the rest
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
        ctx = sample_context(base_url, row.trigger)
        contexts = {c: ctx for c in on}
        heads = {c: "" for c in on}
        lines = _sample_lines(row)
    else:
        trip = preview_trip(row.trigger, lead, service_types)
        tp = TouchPoint(lead=lead, reservation=trip, kind=row.key)
        people = rules.recipients_for(tp, row, recipients.resolve(row, lead, trip))
        lines = _to_lines(row, people)
        contexts, heads = {}, {}
        samples = sample_context(base_url, row.trigger)
        for channel in on:
            claimed = _claimed(people, channel)
            first = next((p for p in people if id(p) in claimed), None)
            ctx = build_context(lead, trip)
            # A staff click's own values come from its caller; samples stand in for them.
            ctx.update({key: samples[key] for key in message_fields.CLICK_FIELDS})
            if first is not None and row.audience in (
                MessageTemplate.Audience.CUSTOMER,
                MessageTemplate.Audience.PLANNER,
            ):
                ctx["first_name"] = (first.name or "").split(" ")[0] or "there"
            rules.fill_links(tp, row, ctx, base_url)
            contexts[channel] = ctx
            heads[channel] = _label(row, first) if first is not None else ""
    if recipients_locked(row):
        lines = [WHO[row.key]]
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
