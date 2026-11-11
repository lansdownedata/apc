"""The trigger catalog: every moment a message can be sent on, and what each one allows.

A fixed dict in code. A message row names one trigger by key; the trigger says whether it is a
date (with an anchor to time the send from), an app event, a staff click or a schedule; whether
it belongs to the order or one trip; which audiences may use it; which placeholder groups
(`fields._GROUPS`) it can fill; and how it is timed. A staff click's own groups ("Quote",
"Receipt", ...) are values and generated parts its caller hands over (`sender.send_now`).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from django.conf import settings

Kind = Literal["date", "event", "staff_click", "schedule"]
Level = Literal["order", "trip"]

GROUP_LABEL: dict[str, str] = {
    "date": "Dates",
    "event": "Events",
    "staff_click": "Staff clicks",
    "schedule": "Schedules",
}


@dataclass(frozen=True)
class Trigger:
    key: str
    label: str
    kind: Kind
    level: Level
    audiences: frozenset[str]
    field_groups: frozenset[str]
    # "dispatched_with_driver": not sendable until the trip is Dispatched with driver details.
    waits_for: str = ""
    # Date triggers on pickup combine per person per day (spec §5).
    combine: bool = False
    # Date triggers only: the instant the timing counts from, or None when there is none yet.
    anchor: Callable[..., datetime | None] | None = None
    # An event its own service sends at once through `sender.send_now`, with what it hands
    # over (the wedding-details email): nothing queues it, so like a staff click it has no
    # timing, no other row moves onto it, and the order's rules and the master switch don't
    # hold it back.
    caller_sends: bool = False


def _pickup(lead, reservation) -> datetime | None:
    return reservation.pickup_at


def _quote_expiry(lead, reservation) -> datetime | None:
    return lead.quote_expires_at


def _balance_due(lead, reservation) -> datetime | None:
    """Midnight company time on the balance due date, as the payment reminder is timed today."""
    plan = getattr(lead, "payment", None)
    due = getattr(plan, "balance_due_date", None)
    if due is None:
        return None
    return datetime.combine(due, time.min, ZoneInfo(settings.TIME_ZONE))


CUSTOMER, AFFILIATE, PLANNER, STAFF = "customer", "affiliate", "planner", "staff"
DRIVER = "driver"

_ORDER_FIELDS = frozenset({"Customer", "Order", "Payment", "Links", "Company"})
_TRIP_FIELDS = _ORDER_FIELDS | {"Trip"}
_DRIVER_FIELDS = _TRIP_FIELDS | {"Driver"}
_AFFILIATE_FIELDS = frozenset({"Trip", "Affiliate", "Payout", "Links", "Company"})

# Who may hear about an order or a trip, unless the trigger says otherwise.
_PEOPLE = frozenset({CUSTOMER, PLANNER, STAFF})


def _t(key: str, label: str, kind: Kind, level: Level, audiences, groups, **extra) -> Trigger:
    return Trigger(key, label, kind, level, frozenset(audiences), frozenset(groups), **extra)


_ALL: tuple[Trigger, ...] = (
    # Dates
    _t(
        "pickup",
        "Pickup",
        "date",
        "trip",
        {CUSTOMER, AFFILIATE, PLANNER, STAFF},
        _TRIP_FIELDS | {"Affiliate", "Payout"},
        combine=True,
        anchor=_pickup,
    ),
    _t(
        "pickup_dispatched",
        "Pickup, once dispatched",
        "date",
        "trip",
        _PEOPLE,
        _DRIVER_FIELDS,
        waits_for="dispatched_with_driver",
        combine=True,
        anchor=_pickup,
    ),
    _t(
        "quote_expiry",
        "Quote expires",
        "date",
        "order",
        _PEOPLE,
        _ORDER_FIELDS,
        anchor=_quote_expiry,
    ),
    _t(
        "balance_due",
        "Balance due",
        "date",
        "order",
        _PEOPLE,
        _ORDER_FIELDS,
        anchor=_balance_due,
    ),
    # Events, order level
    _t("lead_created", "Lead comes in", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t(
        "wedding_details_received",
        "Wedding details received",
        "event",
        "order",
        _PEOPLE,
        _ORDER_FIELDS | {"Wedding details"},
        caller_sends=True,
    ),
    _t("quote_sent", "Quote sent", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t("quote_viewed", "Quote viewed", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t("order_confirmed", "Order confirmed", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t("order_cancelled", "Order cancelled", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t(
        "deposit_hold_released",
        "Deposit hold released",
        "event",
        "order",
        _PEOPLE,
        _ORDER_FIELDS,
    ),
    _t("deposit_paid", "Deposit paid", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t("balance_paid", "Balance paid", "event", "order", _PEOPLE, _ORDER_FIELDS),
    _t("all_trips_done", "All trips done", "event", "order", _PEOPLE, _ORDER_FIELDS),
    # Events, trip level
    _t(
        "affiliate_accepted",
        "Affiliate accepts",
        "event",
        "trip",
        {CUSTOMER, AFFILIATE, STAFF},
        _TRIP_FIELDS | {"Affiliate", "Payout"},
    ),
    _t("status_dispatched", "Status: Dispatched", "event", "trip", _PEOPLE, _DRIVER_FIELDS),
    _t("status_on_the_way", "Status: On the way", "event", "trip", _PEOPLE, _DRIVER_FIELDS),
    _t("status_arrived", "Status: Arrived", "event", "trip", _PEOPLE, _DRIVER_FIELDS),
    _t("driver_changed", "Driver details change", "event", "trip", _PEOPLE, _DRIVER_FIELDS),
    _t(
        "trip_cancelled",
        "Trip cancelled",
        "event",
        "trip",
        {CUSTOMER, AFFILIATE, PLANNER, STAFF},
        _TRIP_FIELDS | {"Affiliate", "Payout"},
    ),
    _t("trip_done", "Trip done", "event", "trip", _PEOPLE, _DRIVER_FIELDS),
    # The driver link (`dispatch.driver_link.send_link`): sent by the dispatch service the
    # moment coverage is confirmed, so the drawer can say whether it went. The vendor's
    # email carries the pickup day its offer did ("Affiliate click"); our own driver's text
    # gets the trip and no order, payment or customer-link fields: no money reaches a driver, and
    # the vendor's email carries no payout or customer link either.
    _t(
        "coverage_confirmed",
        "Coverage confirmed",
        "event",
        "trip",
        {AFFILIATE},
        {"Trip", "Company", "Affiliate", "Affiliate click", "Driver link"},
        caller_sends=True,
    ),
    _t(
        "driver_assigned",
        "Driver assigned",
        "event",
        "trip",
        {DRIVER},
        {"Trip", "Company", "Driver link"},
        caller_sends=True,
    ),
    # Staff clicks
    _t(
        "send_quote",
        "Send quote",
        "staff_click",
        "order",
        {CUSTOMER, PLANNER},
        _ORDER_FIELDS | {"Quote"},
    ),
    # The payment and terms links: a text to anyone with a mobile, else an email, recorded on
    # the customer's conversation.
    _t(
        "send_pay_link",
        "Send payment link",
        "staff_click",
        "order",
        {CUSTOMER, PLANNER},
        _ORDER_FIELDS | {"Payment link"},
    ),
    _t(
        "send_terms_link",
        "Send terms link",
        "staff_click",
        "order",
        {CUSTOMER, PLANNER},
        _ORDER_FIELDS | {"Terms"},
    ),
    _t(
        "payment_receipt",
        "Payment receipt",
        "staff_click",
        "order",
        {CUSTOMER, PLANNER},
        _ORDER_FIELDS | {"Receipt"},
    ),
    _t(
        "affiliate_offer",
        "Offer trip to affiliate",
        "staff_click",
        "trip",
        {AFFILIATE},
        _AFFILIATE_FIELDS | {"Affiliate click"},
    ),
    _t(
        "affiliate_cancel",
        "Cancel trip with affiliate",
        "staff_click",
        "trip",
        {AFFILIATE},
        _AFFILIATE_FIELDS | {"Affiliate click", "Cancellation"},
    ),
    _t(
        "planner_invite",
        "Planner invite",
        "staff_click",
        "order",
        {PLANNER},
        {"Company", "Account", "Planner account"},
    ),
    _t(
        "planner_password_reset",
        "Planner password reset",
        "staff_click",
        "order",
        {PLANNER},
        {"Company", "Account", "Planner account"},
    ),
    _t(
        "user_invite",
        "Staff user invite",
        "staff_click",
        "order",
        {STAFF},
        {"Company", "Account", "Staff access"},
    ),
    # Schedules
    _t(
        "deposit_report",
        "Deposit report",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
    _t(
        "review_report",
        "Review report",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
    _t(
        "complaint_report",
        "Complaint report",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
    _t(
        "unconfirmed_trips_report",
        "Unconfirmed trips report",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
    _t(
        "task_digest",
        "Task digest",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
    _t(
        "dispatch_alert",
        "Dispatch alert",
        "schedule",
        "order",
        {STAFF},
        {"Company"},
    ),
)

CATALOG: dict[str, Trigger] = {t.key: t for t in _ALL}


def get(key: str) -> Trigger:
    """The trigger for a key; KeyError when there is none."""
    return CATALOG[key]
