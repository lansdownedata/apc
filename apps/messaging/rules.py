"""Built-in rules and send-time checks (spec C sections 3, 4 and 9).

`check` decides, when a queued TouchPoint comes due, whether it goes out: first the safety
rules every row obeys, then the special cases a seeded message keeps while it stays on its
original trigger. `recipients_for`, `combine_key` and `fill_links` carry the rest of those
special cases for the sender. Everything here was ported from the old
engine's send-time code and keeps its reason strings word for word: the order's message
history shows them, and the golden recording pins them.
"""

import logging
from typing import Literal

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.core.templatetags.phone_filters import phone_display
from apps.reservations.models import Reservation

from . import defaults, fields, triggers
from .models import Delivery, MessageTemplate, NotificationConfig, TouchPoint
from .recipients import Recipient, _person, resolve

logger = logging.getLogger(__name__)

_Trip = Reservation.TripStatus
# Trip statuses at which the customer may be told who is driving: from Dispatched onward.
DRIVER_MESSAGE_STATUSES = frozenset(
    {
        _Trip.DISPATCHED,
        _Trip.ON_THE_WAY,
        _Trip.ARRIVED,
        _Trip.CUSTOMER_IN_CAR,
    }
)

Verdict = Literal["send", "skip", "cancel", "wait"]
# ("send", ""), ("skip", reason), ("cancel", reason), or ("wait", reason): leave it SCHEDULED.
Check = tuple[Verdict, str]

SEND: Check = ("send", "")
DISABLED = "message disabled"
SERVICE_TYPE_MISMATCH = "service type no longer matches"
# The TouchPoint error when every person a combining send would reach already had that
# day's message. Combining is the sender's step (it knows the addresses); this is the
# string it writes.
COVERED = "covered by the grouped notice"

_QUOTE_PROGRAM = frozenset(
    {
        "tp1_welcome",
        "tp2_lead_followup",
        "tp3_quote_sent_sms",
        "tp4_viewed_sms",
        "tp5_viewed_email",
        "tp6_quote_followup",
        "tp7_expiring",
        "tp8_expired",
    }
)
_DEPOSIT_STOPS = _QUOTE_PROGRAM - {"tp1_welcome", "tp2_lead_followup"}
CONFIRMS = frozenset({"trip_confirm_customer", "trip_confirm_customer_2"})
FIRST_NOTICE, SECOND_NOTICE = "trip_confirm_customer", "trip_confirm_customer_2"
# Why the second notice stood down: the first went at or after its own send time (MSG-09).
FIRST_JUST_SENT = "First notice just sent"
_THANK_YOUS = frozenset({"thank_you", "thank_you_wedding"})
# Seeded rows on a combining trigger that today send one message per trip, each with its
# own trip's link or details.
_PER_TRIP = frozenset({"wed_final_details", "trip_confirm_affiliate", "driver_released"})
# The placeholder a combining send fills with every trip the person has that day
# (`sender.context`), and the one that lists each trip with its driver (APC-131): the
# only list a row that waits for dispatch may combine on, since it carries every trip's
# driver. A trip's driver line is released once that trip is Dispatched with the details.
DAY_LIST = "trip_sheet"
DRIVER_LIST = "trip_sheet_drivers"
DRIVER_TO_FOLLOW = "Driver: details to follow"
# How a combine key covering a whole order starts (`combine_key`).
ORDER_KEY = "order:"
# Seeded rows that wait for a condition their trigger doesn't carry.
_WAITS = {"driver_updated": "dispatched_with_driver"}
_CANCELLATIONS = frozenset({"trip_cancelled", "order_cancelled"})
# Placeholders built on PUBLIC_BASE_URL; `review_link` comes from Podium instead.
_SITE_LINKS = frozenset({"quote_link", "pay_link", "confirm_link", "ack_link"})

_CONFIRM_RULES = [
    "Weddings and orders with a cleaning-fee vehicle go to the billing contact only,"
    " with the acknowledgment link",
    "Their notice never stands in for the booker's other trips that day",
    "Skips a day already confirmed (for those orders, once the fees are acknowledged too)",
]
_DESCRIPTIONS: dict[str, list[str]] = {
    **{
        key: ["Stops once the order is booked, waiting on confirmation or lost"]
        for key in _QUOTE_PROGRAM - _DEPOSIT_STOPS
    },
    **{
        key: [
            "Stops once the order is booked, waiting on confirmation or lost",
            "Stops once the deposit is taken",
        ]
        for key in _DEPOSIT_STOPS
    },
    "payment_reminder": [
        "Skips orders billed to an account",
        "Skips orders with no card on file",
        "Skips plans already paid",
    ],
    FIRST_NOTICE: _CONFIRM_RULES,
    SECOND_NOTICE: [
        *_CONFIRM_RULES,
        "Skips when the first notice went at or after its time (a late booking)",
    ],
    "wed_final_details": ["Links to the wedding details form", "One message per order"],
    "trip_confirm_affiliate": [
        "Skips once the affiliate no longer holds the trip; not sent for GNet or in-house coverage",
        "Links to the affiliate's acknowledgment page",
        "One message per trip",
    ],
    "driver_released": [
        "Waits for Dispatched with the driver's details",
        "One message per trip",
    ],
    "driver_updated": ["Waits for Dispatched with the driver's details"],
    "status_dispatched": ["Retired: the driver details message covers dispatch"],
    "thank_you": [
        "Booked orders only",
        "Wedding orders get the wedding thank-you instead",
    ],
    "thank_you_wedding": [
        "Booked orders only",
        "Also sends for an order with a wedding name",
    ],
    "review_request": [
        "Uses Podium's review invite",
        "Held after a complaint or a low rating",
    ],
    # The button sends (APC-129): the row says who, the button how and when.
    "send_quote": ["By text and email, as staff choose when they click Send quote"],
    "send_quote_on_account": ["By text and email, as staff choose when they click Send quote"],
    "send_pay_link": ["By text to anyone with a mobile, else by email"],
    "send_terms_link": ["By text to anyone with a mobile, else by email"],
    "payment_receipt": ["Goes to the booking contact when the billing contact has no email"],
    "affiliate_offer": ["Goes to the affiliate being offered the trip"],
    "affiliate_cancel": ["Goes to the affiliate the trip is taken back from"],
}


def has_built_ins(row: MessageTemplate) -> bool:
    """A seeded row keeps its built-in rules only while it stays on its original trigger."""
    default = defaults.ORIGINALS.get(row.key)
    return bool(row.seeded and default is not None and row.trigger == default.trigger)


# The Podium links: each person is texted when they have a mobile, else emailed.
TEXT_FIRST = frozenset({"send_pay_link", "send_terms_link"})


def text_first(row: MessageTemplate) -> bool:
    """Whether a row's send texts each person with a mobile and emails the rest (the pay
    and terms links, on their own trigger)."""
    return row.key in TEXT_FIRST and has_built_ins(row)


def combines_per_trip(key: str) -> bool:
    """True for a built-in message whose own rule is one message per trip (on its original
    trigger), overriding the trigger's per-day combining."""
    return key in _PER_TRIP


def uses_field(row: MessageTemplate, name: str) -> bool:
    """Whether the wording of any channel the row sends on uses the placeholder ``name``."""
    texts = []
    if row.send_email:
        texts += [row.subject, row.email_body]
    if row.send_sms:
        texts.append(row.sms_body)
    for text in texts:
        try:
            if name in fields._names(text or ""):
                return True
        except ValueError:  # unbalanced braces: the editor refuses these
            continue
    return False


def combines_per_day(row: MessageTemplate) -> bool:
    """Whether a row on a combining trigger sends one message per person per day (spec
    section 5): the first send lists the person's trips that day, and a later trip's send
    to them is skipped as already sent.

    A built-in on its own trigger keeps its own rule: the confirmations combine, the
    per-trip messages do not. Any other row combines only when its wording lists the day
    (`DAY_LIST`): without the list, skipping a later trip would drop that trip's details.
    A row that waits for dispatch combines only on the list that carries every trip's
    driver (`DRIVER_LIST`, APC-131), and an affiliate's sheet is one trip's, so an
    affiliate row never combines."""
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is None or not trigger.combine:
        return False
    if has_built_ins(row):
        return row.key not in _PER_TRIP
    if row.audience == MessageTemplate.Audience.AFFILIATE:
        return False
    if trigger.waits_for:
        return uses_field(row, DRIVER_LIST)
    return uses_field(row, DAY_LIST)


def driver_sheet(reservation) -> str:
    """The trip's sheet with its driver's name and cell under it, once the trip is
    Dispatched with the details on file; until then the sheet says they are to follow."""
    from apps.reservations.services import trip_sheet_text

    sheet = trip_sheet_text(reservation)
    if not ready(reservation, "dispatched_with_driver"):
        return f"{sheet}\n  {DRIVER_TO_FOLLOW}"
    info = confirmed_assignment(reservation).driver_info or {}
    cell = phone_display(info.get("cell", ""))
    who = " · ".join(part for part in (info.get("name", ""), cell) if part)
    return f"{sheet}\n  Driver: {who}"


def built_in_descriptions(key: str) -> list[str]:
    """The built-in rules of a seeded message, as the editor lists them (read-only)."""
    return list(_DESCRIPTIONS.get(key, []))


def _is_wedding(reservation) -> bool:
    from apps.reservations.services import is_wedding_trip

    return is_wedding_trip(reservation)


def _order_keyed(reservation, cache: dict | None = None) -> bool:
    """`reservations.services.is_order_keyed`: a wedding's or a held order's confirmation
    goes through the order's own link, to the billing contact."""
    from apps.reservations.services import is_order_keyed

    return is_order_keyed(reservation, cache)


def _held_cache(tp: TouchPoint) -> dict | None:
    """`_order_keyed`'s cache for one send (the sender's `send_cache`), None outside one."""
    cache = getattr(tp, "send_cache", None)
    return cache.setdefault("held", {}) if cache is not None else None


def confirmed_assignment(reservation):
    """The trip's active coverage (`dispatch.selectors.confirmed_assignment`)."""
    from apps.dispatch import selectors

    return selectors.confirmed_assignment(reservation)


def trip_matches(trip, types) -> bool:
    """Whether a trip is of one of these service types (Wedding counts legacy wedding legs)."""
    for st in types:
        if st.name.strip().lower() == defaults.WEDDING_SERVICE_NAME.lower():
            if _is_wedding(trip):  # legacy wedding-builder legs count too
                return True
        elif trip.service_type_id == st.pk:
            return True
    return False


def matches(row: MessageTemplate, lead, reservation=None) -> bool:
    """The row's service-type filter (spec section 3). Empty matches everything; a trip-level
    row matches on its own trip, an order-level row on any live trip of the order. The
    seeded wedding thank-you also matches an order with a wedding name."""
    trigger = triggers.CATALOG.get(row.trigger)
    return types_match(
        list(row.service_types.all()),
        lead,
        reservation,
        trip_level=trigger is not None and trigger.level == "trip",
        wedding_name=wedding_name_counts(row),
    )


def wedding_name_counts(row: MessageTemplate) -> bool:
    """The seeded wedding thank-you, on its own trigger, also goes to an order that has only
    a wedding name."""
    return row.key == "thank_you_wedding" and has_built_ins(row)


def types_match(
    types: list, lead, reservation=None, *, trip_level: bool, wedding_name: bool = False
) -> bool:
    """`matches` for a list of service types (the editor's draft has no saved ones)."""
    from apps.dispatch.selectors import CANCELLED_STATUSES

    if not types:
        return True
    if wedding_name and lead.wedding_name:
        return True
    if trip_level and reservation is not None:
        trips = [reservation]
    else:
        trips = [r for r in lead.reservations.all() if r.trip_status not in CANCELLED_STATUSES]
    return any(trip_matches(t, types) for t in trips)


def ready(reservation, waits_for: str, *, dispatch_config=None) -> bool:
    """Whether a waiting row's condition holds now. A cancelled trip is never ready, and
    neither is a condition this module doesn't know. ``dispatch_config`` is the
    `DispatchAlertConfig` a caller already read."""
    from apps.dispatch.models import DispatchAlertConfig
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.dispatch.services import driver_details_ready

    if waits_for != "dispatched_with_driver" or reservation.trip_status in CANCELLED_STATUSES:
        return False
    if reservation.trip_status not in DRIVER_MESSAGE_STATUSES:
        return False
    assignment = confirmed_assignment(reservation)
    if assignment is None:
        return False
    return driver_details_ready(assignment, dispatch_config or DispatchAlertConfig.load())


# Rows whose sends carry the driver's details (`sender._DRIVER_TRIGGERS`), and the old
# scheduler's kinds for them.
_DRIVER_TRIGGERS = ("pickup_dispatched", "driver_changed")
_DRIVER_KINDS = ("driver_released", "driver_updated")


def last_told_driver(reservation, *, before=None) -> TouchPoint | None:
    """The trip's newest driver message that reached anyone (sent, or still holding its
    texts for quiet hours: its email went), so its `driver_details` are what the customer
    was last told. ``before`` only counts one that reached them before that moment."""
    rows = MessageTemplate.objects.filter(trigger__in=_DRIVER_TRIGGERS)
    reached = Delivery.objects.filter(touchpoint=OuterRef("pk"), status=Delivery.Status.SENT)
    sent = TouchPoint.objects.annotate(partly=Exists(reached)).filter(
        Q(message__in=rows) | Q(message__isnull=True, kind__in=_DRIVER_KINDS),
        Q(status=TouchPoint.Status.SENT) | Q(status=TouchPoint.Status.SCHEDULED, partly=True),
        reservation=reservation,
    )

    def told_at(tp: TouchPoint):
        return tp.sent_at or tp.created_at

    if before is not None:
        sent = [tp for tp in sent if told_at(tp) < before]
    return max(sent, key=lambda tp: (told_at(tp), tp.pk), default=None)


def _driver_unchanged(tp: TouchPoint) -> bool:
    """A driver-change notice whose details are back to what its readers were told before
    it was queued: there is no change to report. Only notices that reached someone before
    this one was queued count, so a sibling row's notice for the same change (any audience)
    never stands in for it. A notice that already reached someone (its email went, its
    texts wait for quiet hours) always finishes, so all its readers hear the same details.
    Unknown details (a send made before they were recorded, or no coverage now) never
    count as unchanged."""
    reached = (
        tp.driver_details is not None or tp.deliveries.filter(status=Delivery.Status.SENT).exists()
    )
    if reached:
        return False
    newest = last_told_driver(tp.reservation, before=tp.created_at)
    if newest is None or newest.driver_details is None:
        return False
    assignment = confirmed_assignment(tp.reservation)
    return assignment is not None and newest.driver_details == assignment.driver_info


def _uses_site_link(row: MessageTemplate) -> bool:
    texts = []
    if row.send_email:
        texts += [row.subject, row.email_body]
    if row.send_sms:
        texts.append(row.sms_body)
    for text in texts:
        try:
            if fields._names(text or "") & _SITE_LINKS:
                return True
        except ValueError:  # unbalanced braces: the editor refuses these; nothing to fill
            continue
    return False


def _review_hold_reason(lead) -> str:
    """Why this order's review request shouldn't go out (decision D3), or "". We don't ask
    an unhappy customer for a public review; a person follows up instead."""
    from apps.leads.models import CustomerFeedback
    from apps.reservations.models import TripIssue

    if TripIssue.objects.filter(
        review__reservation__lead=lead, category=TripIssue.Category.COMPLAINT
    ).exists():
        return "complaint logged"
    feedback = CustomerFeedback.objects.filter(lead=lead).first()
    if feedback is not None and feedback.is_low:
        return "low rating"
    return ""


def confirm_group(tp: TouchPoint) -> list:
    """The trips one customer confirmation covers. An order-keyed one (a wedding or a held
    order, `_order_keyed`) covers its own order that day; any other covers the rest of the
    booker's day, order-keyed trips left out, so the two never stand in for each other.

    Read once per send: the sender gives the TouchPoint a `send_cache` dict for the length
    of one `deliver`, which this fills; without one it is read fresh every time."""
    from apps.reservations.services import trip_day_group, wedding_day_group

    cache = getattr(tp, "send_cache", None)
    if cache is not None and "confirm_group" in cache:
        return cache["confirm_group"]
    day = tp.reservation.pickup_date
    held = _held_cache(tp)
    if held is None:
        held = {}
    if _order_keyed(tp.reservation, held):
        group = wedding_day_group(tp.lead, day)
    else:
        group = [t for t in trip_day_group(tp.lead.contact, day) if not _order_keyed(t, held)]
    if cache is not None:
        cache["confirm_group"] = group
    return group


def _day_confirmed(tp: TouchPoint) -> bool:
    group = confirm_group(tp)
    fees_owed = tp.lead.fees_acknowledged_at is None and _order_keyed(
        tp.reservation, _held_cache(tp)
    )
    return bool(group) and all(t.customer_confirmed_at is not None for t in group) and not fees_owed


def _first_notice_since(tp: TouchPoint) -> bool:
    """The first notice told the customer about this trip at or after the second notice's
    own send time: it went late (a booking inside 48 hours, a catch-up), so the reminder
    would land on top of it (MSG-09). Read from its sent Deliveries, so a first notice
    whose texts quiet hours still hold counts once its email went; one on another trip of
    the day counts when it listed this one."""
    if tp.scheduled_for is None:
        return False
    trips = [t.pk for t in confirm_group(tp)] or [tp.reservation_id]
    sent = (
        Delivery.objects.filter(
            status=Delivery.Status.SENT,
            created_at__gte=tp.scheduled_for,
            touchpoint__reservation__in=trips,
        )
        .filter(
            Q(touchpoint__message__key=FIRST_NOTICE)
            | Q(touchpoint__message__isnull=True, touchpoint__kind=FIRST_NOTICE)
        )
        .exclude(touchpoint__status=TouchPoint.Status.CANCELLED)
        .values_list("touchpoint__reservation_id", "covers")
    )
    return any(
        res == tp.reservation_id or tp.reservation_id in (covers or []) for res, covers in sent
    )


def _payment_reminder_reason(lead) -> str:
    from apps.payments.models import PaymentPlan
    from apps.payments.services import remaining_balance

    plan = getattr(lead, "payment", None)
    if lead.on_account:
        # Checked when due, not cancelled at the switch: an order switched back to card
        # before this fires still gets its notice (APC-105).
        return "billed to an account"
    if plan is None or not plan.stripe_payment_method_id:
        return "no card on file"
    if plan.balance_status != PaymentPlan.BalanceStatus.SCHEDULED:
        return "balance not scheduled"
    if remaining_balance(lead) <= 0:
        return "nothing owed"
    return ""


def _wedding_thank_you_instead(lead) -> bool:
    """The wedding thank-you went out for this order, or is queued and still able to, so
    the plain one stands down. Without a queued one (queued before the cut-over, or the row
    switched on after the trips finished) the customer gets the plain thank-you."""
    wedding = TouchPoint.objects.filter(lead=lead, kind="thank_you_wedding").exclude(
        status__in=(TouchPoint.Status.CANCELLED, TouchPoint.Status.SKIPPED)
    )
    if wedding.exclude(status=TouchPoint.Status.SCHEDULED).exists():
        return True
    if not wedding.exists():
        return False
    row = MessageTemplate.objects.filter(key="thank_you_wedding").first()
    return row is not None and row.enabled and row.archived_at is None and matches(row, lead)


def _affiliate_coverage_skip(reservation) -> str:
    """Only a manual farm-out is confirmed by the affiliate (APC-20). A GNet vendor's
    acceptance is its acknowledgement, and in-house coverage has no affiliate. The row is
    queued once a manual vendor confirms (a re-confirmation queues a fresh one), so
    coverage gone by send time closes it, with today's reason."""
    from apps.dispatch.models import Assignment

    assignment = confirmed_assignment(reservation)
    if assignment is None:
        return "no confirmed affiliate"
    if assignment.is_in_house:
        return "no affiliate confirmation for in-house coverage"
    if assignment.channel == Assignment.Channel.GNET:
        return "no affiliate confirmation for GNet coverage"
    if not assignment.vendor_id:
        return "no confirmed affiliate"
    return ""


def _built_in_skip(tp: TouchPoint, row: MessageTemplate) -> str:
    """The skip reason from a seeded row's own rules, or ""."""
    from apps.payments.models import PaymentPlan

    lead, key = tp.lead, row.key
    if key in _QUOTE_PROGRAM:
        if lead.status == lead.Status.BOOKED:
            return "lead BOOKED"
        if lead.status == lead.Status.ENGAGED:
            # They authorized and are waiting on us (APC-26): never chase them to book.
            return "lead ENGAGED"
        plan = getattr(lead, "payment", None)
        if (
            key in _DEPOSIT_STOPS
            and plan is not None
            and plan.deposit_status
            in (
                PaymentPlan.DepositStatus.PAID,
                PaymentPlan.DepositStatus.AUTHORIZED,
            )
        ):
            return "deposit already taken"
    elif key == "payment_reminder":
        return _payment_reminder_reason(lead)
    elif key in CONFIRMS:
        if _day_confirmed(tp):
            return "day already confirmed"
        if key == SECOND_NOTICE and _first_notice_since(tp):
            return FIRST_JUST_SENT
    elif key in _THANK_YOUS:
        if lead.status != lead.Status.BOOKED:
            return "lead not booked"
        if key == "thank_you" and _wedding_thank_you_instead(lead):
            return "wedding thank-you sends instead"
    elif key == "trip_confirm_affiliate":
        return _affiliate_coverage_skip(tp.reservation)
    elif key == "review_request":
        if not any(r.phone for r in recipients_for(tp, row, resolve(row, lead))):
            return "no phone for review invite"
    return ""


def check(tp: TouchPoint, row: MessageTemplate, *, master: bool | None = None) -> Check:
    """Whether `tp` may go out now. Skips and cancels are final; a wait leaves it SCHEDULED.

    A send with no order (an invite, a staff report) and a staff click answer only to the
    row's own switch, and a staff click to its service types.

    ``master`` is the master switch, so a batch reads `NotificationConfig` once. A held
    review request also brings its complaint follow-up task up to date (`tasks.sync`).
    """
    lead, res = tp.lead, tp.reservation
    trigger = triggers.CATALOG.get(row.trigger)
    built_in = has_built_ins(row)

    if master is None:
        master = NotificationConfig.load().enabled
    if not master or not row.enabled or row.archived_at is not None:
        return "skip", DISABLED
    if tp.lead_id is None:
        # An invite or a staff report: there is no order to be lost, cancelled or run.
        return SEND
    if trigger is not None and (trigger.kind == "staff_click" or trigger.caller_sends):
        # A staff click keeps the checks its own service makes today (a refund receipt goes
        # out on a cancelled order, an affiliate hears a trip was released); the order's
        # rules are for the messages nobody clicked. Its links come from the caller. So does
        # an event its service sends itself (a couple on a lost order who send their
        # wedding details again still get them back).
        if not matches(row, lead, res):
            return "skip", SERVICE_TYPE_MISMATCH
        return SEND
    # A cancellation message exists because the order is lost: the order's own, or a
    # trip's when the whole order was cancelled (spec section 3, "unless the trigger is the
    # cancellation itself").
    cancellation = row.trigger in _CANCELLATIONS
    if lead.status == lead.Status.LOST and not cancellation:
        return "skip", "lead LOST"

    if built_in and row.key == "review_request":
        held = _review_hold_reason(lead)
        if held:
            # A complaint's follow-up is a task; make sure it exists now rather than on the
            # next run-tasks tick. A low rating is picked up by the customer review report.
            from apps.tasks import services as tasks

            tasks.sync(lead)
            return "cancel", f"Review request held: {held}"

    waits_for = (trigger.waits_for if trigger else "") or (
        _WAITS.get(row.key, "") if built_in else ""
    )
    if trigger is not None and trigger.level == "trip":
        lost_and_cancelling = cancellation and lead.status == lead.Status.LOST
        if lead.status != lead.Status.BOOKED and not lost_and_cancelling:
            return "skip", "lead not booked"
        if res is None:
            return "skip", "no reservation"
        from apps.dispatch.selectors import CANCELLED_STATUSES

        # A waiting row is held instead (a cancelled trip is not ready), so a reinstated
        # trip still gets it; it closes once pickup passes.
        if (
            not waits_for
            and res.trip_status in CANCELLED_STATUSES
            and row.trigger not in _CANCELLATIONS
        ):
            return "skip", "trip cancelled"
        pickup = res.pickup_at
        if trigger.kind == "date" and pickup is None:
            return "skip", "trip has no pickup date"
        before = trigger.kind == "date" and row.timing_direction == row.Direction.BEFORE
        if pickup is not None and pickup <= timezone.now() and (waits_for or before):
            return "skip", "pickup passed"

    if not matches(row, lead, res):
        return "skip", SERVICE_TYPE_MISMATCH

    if built_in:
        reason = _built_in_skip(tp, row)
        if reason:
            return "skip", reason

    if waits_for and not ready(res, waits_for):
        return "wait", "waiting for Dispatched with the driver's details"
    if row.trigger == "driver_changed" and res is not None and _driver_unchanged(tp):
        return "skip", "no change to report"
    if _uses_site_link(row) and not settings.PUBLIC_BASE_URL:
        logger.warning("touch-point %s (%s) not sent: PUBLIC_BASE_URL is unset", tp.pk, row.key)
        return "wait", "waiting for PUBLIC_BASE_URL"
    return SEND


def recipients_for(
    tp: TouchPoint, row: MessageTemplate, resolved: list[Recipient]
) -> list[Recipient]:
    """The resolved recipients after the built-in override.

    A wedding's or a held order's trip confirmations go to the billing contact only (APC-94,
    APC-95): that page carries the fee acknowledgment the client wants from whoever pays,
    and a planner's tick must never count as the customer's. A farmed-in order keeps its
    operator (spec section 4).
    """
    if (
        has_built_ins(row)
        and row.key in CONFIRMS
        and tp.reservation is not None
        and _order_keyed(tp.reservation, _held_cache(tp))
        and not tp.lead.is_farm_in
    ):
        return [_person(tp.lead.effective_billing_contact, "billing contact")]
    if has_built_ins(row) and row.key == "payment_receipt" and not tp.lead.is_farm_in:
        return _billing_else_booking(tp.lead, resolved)
    return resolved


def _billing_else_booking(lead, resolved: list[Recipient]) -> list[Recipient]:
    """The receipt's rule (APC-129): a billing contact with no email is stood in for by the
    booking contact, once."""
    out: list[Recipient] = []
    for person in resolved:
        if person.label.endswith("(billing contact)") and not person.email:
            person = _person(lead.contact, "booking contact")
            if any(p.contact_id == person.contact_id for p in out):
                continue
        out.append(person)
    return out


def combine_key(tp: TouchPoint, row: MessageTemplate) -> str:
    """What a send on a combining trigger is keyed on per address: the trip's local pickup
    date for a row that combines (`combines_per_day`), an order-keyed confirmation (a
    wedding's or a held order's) adding its order; the trip as well for one that sends per
    trip. The wedding details request is keyed on its order alone: its answers belong to the
    order, so the first leg's send covers every leg, whatever day each is on (APC-124).
    Empty for a row whose trigger doesn't combine."""
    trigger = triggers.CATALOG.get(row.trigger)
    res = tp.reservation
    if trigger is None or not trigger.combine or res is None or res.pickup_date is None:
        return ""
    if has_built_ins(row) and row.key == "wed_final_details":
        return f"{ORDER_KEY}{tp.lead_id}"
    day = res.pickup_date.isoformat()
    if not combines_per_day(row):
        return f"{day}:trip:{res.pk}"
    if has_built_ins(row) and row.key in CONFIRMS and _order_keyed(res, _held_cache(tp)):
        return f"{day}:wedding:{tp.lead_id}"
    return day


def fill_links(tp: TouchPoint, row: MessageTemplate, ctx: dict, base_url: str) -> None:
    """Put the public-page links the row can use into `ctx`."""
    from apps.leads.services import make_pay_page_url, make_quote_page_url
    from apps.reservations.acknowledgements import (
        affiliate_ack_url,
        driver_link_url,
        order_day_ack_url,
        trip_day_ack_url,
        wedding_details_url,
    )

    lead, res = tp.lead, tp.reservation
    key = row.key if has_built_ins(row) else ""
    ctx["quote_link"] = make_quote_page_url(lead, base_url=base_url)
    ctx["pay_link"] = make_pay_page_url(lead, base_url=base_url)
    if key in _THANK_YOUS:
        # The trip page for the order's last day: once the order has finished, that page is
        # the rating form (APC-63).
        days = [r.pickup_date for r in lead.reservations.all() if r.pickup_date]
        if days:
            ctx["confirm_link"] = trip_day_ack_url(lead.contact, max(days), base_url=base_url)
        return
    if res is None:
        return
    if key == "wed_final_details":
        ctx["confirm_link"] = wedding_details_url(res, base_url=base_url)
    elif key == "trip_confirm_affiliate":
        assignment = confirmed_assignment(res)
        if assignment is not None:
            ctx["ack_link"] = affiliate_ack_url(assignment, base_url=base_url)
            ctx["driver_link"] = driver_link_url(assignment, base_url=base_url)
    elif key in defaults.DRIVER_LINKS:
        # Its sender hands over the link for the coverage it is about; this is the trip's
        # confirmed coverage, for the editor's preview.
        assignment = confirmed_assignment(res)
        if assignment is not None:
            ctx["driver_link"] = driver_link_url(assignment, base_url=base_url)
    elif key in CONFIRMS and _order_keyed(res, _held_cache(tp)):
        ctx["confirm_link"] = order_day_ack_url(lead, res.pickup_date, base_url=base_url)
    elif row.audience != row.Audience.AFFILIATE:
        ctx["confirm_link"] = trip_day_ack_url(lead.contact, res.pickup_date, base_url=base_url)
