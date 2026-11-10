"""The fields an owner may put in a message, per trigger, and the save-time check on a draft.

`fields_for` / `validate(row_data)` are the editor's. The kind-based `fields_for_kind` and
`validate(kind, ...)` served spec B's settings page (replaced by the editor, APC-119); only
their tests still call them.
"""

import string
from dataclasses import dataclass

from . import defaults, triggers


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str


_GROUPS: dict[str, list[tuple[str, str]]] = {
    "Customer": [("first_name", "First name")],
    "Order": [
        ("quote_no", "Order number"),
        ("pickup_date", "First pickup date"),
        ("pickup_time", "First pickup time"),
        ("pickup_address", "First pickup address"),
        ("dropoff_address", "Final drop-off"),
        ("quote_breakdown", "Trip & price summary"),
        ("agent_first", "Agent first name"),
    ],
    "Payment": [
        ("balance_amount", "Balance due"),
        ("balance_due_date", "Balance due date"),
        ("card_last4", "Card last four"),
    ],
    "Trip": [
        ("trip_pickup_date", "Pickup date"),
        ("trip_pickup_time", "Pickup time"),
        ("trip_pickup_tz", "Time zone"),
        ("trip_pickup_when", "Pickup date & time"),
        ("trips_coming_up", "Trip(s) coming up, as a sentence"),
        ("trip_routing", "Route"),
        ("trip_passengers", "Number of passengers"),
        ("trip_passenger_names", "Passengers"),
        ("trip_vehicle", "Vehicle type"),
        ("trip_sheet", "Trip details"),
        ("contact_on_file", "Day-of contact"),
    ],
    "Driver": [
        ("driver_name", "Driver name"),
        ("driver_cell", "Driver cell"),
        ("vehicle_description", "Vehicle"),
        ("vehicle_number", "Vehicle number"),
        ("trip_sheet_drivers", "Trip details with drivers"),
    ],
    "Affiliate": [("vendor_name", "Affiliate name"), ("payout", "Payout")],
    "Links": [
        ("quote_link", "Quote link"),
        ("pay_link", "Payment link"),
        ("confirm_link", "Confirm link"),
        ("ack_link", "Acknowledge link"),
        ("review_link", "Review link"),
    ],
    "Company": [
        ("company_name", "Company name"),
        ("company_phone", "Company phone"),
        ("company_email", "Company email"),
    ],
    # Filled by a staff click's caller (`sender.send_now`), on its own trigger only. The
    # summaries, sheets and the button are generated blocks: alone on a line in an email they
    # render as their own panel. Inside a click the `trip_sheet` block stands in for the
    # Trip group's `trip_sheet` text, so an offer shows the affiliate's sheet with flights.
    "Quote": [
        ("contact_name", "Customer full name"),
        ("quote_total", "Quote total"),
        ("deposit_pct", "Deposit percent"),
        ("deposit_amount", "Deposit amount"),
        ("quote_valid_through", "Quote valid through"),
        ("quote_summary", "Quote summary"),
        ("button", "View-quote button"),
    ],
    "Payment link": [("payment_for", "What it pays for")],
    "Terms": [("terms_link", "Terms link")],
    "Receipt": [
        ("receipt_name", "Receipt name and number"),
        ("receipt_intro", "Opening sentence"),
        ("receipt_lines", "Receipt lines"),
        ("receipt_explanation", "What the money was for"),
        ("receipt_kind", "Charge or refund"),
    ],
    "Affiliate click": [("trip_day", "Pickup day")],
    "Cancellation": [("cancelled_trip_sheet", "Trip details (no money)")],
    # The invites and the planner password reset have no order: each trigger offers only
    # what its caller fills (`sender.context` adds the company). Every one has the button
    # that sets the password (in plain text, the link itself); a planner email greets by
    # full name, a staff invite by first name and says the access level.
    "Account": [("account_button", "Set-password button")],
    "Planner account": [("account_name", "Full name")],
    # The access-level panel is HTML only (the plain-text part says it in the sentence).
    "Staff access": [
        ("account_first_name", "First name"),
        ("role_label", "Access level"),
        ("access_level", "Access level panel"),
    ],
    # The email a couple gets when they send their wedding details (`public.services`).
    "Wedding details": [
        ("form_name", "Name on the form"),
        ("wedding_details", "Every answer they gave"),
        ("resume_button", "Update-details button"),
    ],
    # A staff report's numbers and dates, and its table, from the report's job. Each report
    # fills only its own (`REPORT_FIELDS`); a count comes with its noun ("2 orders").
    "Report": [
        ("report_date", "Report date"),
        ("report_time", "Report time"),
        ("period_start", "Period start"),
        ("period_end", "Period end"),
        ("orders", "Orders listed"),
        ("booked_orders", "Booked orders listed"),
        ("window_days", "Days to pickup"),
        ("customers", "Customers listed"),
        ("trips", "Trips listed"),
        ("trip_count", "Number of trips"),
        ("asked_count", "Asked for a review"),
        ("held_count", "Review requests held"),
        ("reviewed_count", "Left a review"),
        ("trip_page_ratings", "Trip-page ratings sentence"),
        ("new_complaints", "New complaints"),
        ("overdue_tasks", "Overdue tasks"),
        ("tasks", "Tasks listed"),
        ("critical_mark", "CRITICAL mark (when any is critical)"),
        ("alert_summary", "Exceptions needing attention"),
        ("exceptions", "Exceptions listed"),
        ("critical_note", "Critical count (when any)"),
        ("report_table", "Report table"),
    ],
}

# What each staff report's job fills, on its own seeded row. Only that row is ever sent on
# its schedule trigger (`editor.can_pick`), so the values belong to the report, not the
# trigger.
REPORT_FIELDS: dict[str, frozenset[str]] = {
    "deposit_report": frozenset(
        {"report_date", "orders", "booked_orders", "window_days", "report_table"}
    ),
    "unconfirmed_trips_report": frozenset(
        {"report_date", "customers", "trips", "trip_count", "report_table"}
    ),
    "review_report": frozenset(
        {
            "period_start",
            "period_end",
            "orders",
            "asked_count",
            "held_count",
            "reviewed_count",
            "trip_page_ratings",
            "report_table",
        }
    ),
    "complaint_report": frozenset({"new_complaints", "period_start", "report_table"}),
    "task_digest": frozenset({"report_date", "overdue_tasks", "tasks", "report_table"}),
    "dispatch_alert": frozenset(
        {
            "report_time",
            "critical_mark",
            "alert_summary",
            "exceptions",
            "critical_note",
            "report_table",
        }
    ),
}

# The groups a staff click's (or another `send_now`) caller fills; nothing else does.
_CLICK_GROUPS = (
    "Quote",
    "Payment link",
    "Terms",
    "Receipt",
    "Affiliate click",
    "Cancellation",
    "Account",
    "Planner account",
    "Staff access",
    "Wedding details",
    "Report",
)
CLICK_FIELDS = frozenset(key for group in _CLICK_GROUPS for key, _ in _GROUPS[group])

FIELDS: dict[str, Field] = {
    key: Field(key, label, group) for group, items in _GROUPS.items() for key, label in items
}

_LINKS = (
    "quote_link",
    "pay_link",
    "confirm_link",
    "ack_link",
    "review_link",
    "terms_link",
    "account_button",
    "resume_button",
    "report_table",
)
# Links the sender fills only in particular cases; `fields_for` adds each where it can.
_SENDER_LINKS = ("confirm_link", "ack_link", "review_link")
_TRIP_KINDS = frozenset(
    {
        "wed_final_details",
        "trip_confirm_customer",
        "trip_confirm_customer_2",
        "driver_released",
        "driver_updated",
        "status_dispatched",
        "status_on_the_way",
        "status_arrived",
    }
)
_DRIVER_KINDS = frozenset(
    {
        "driver_released",
        "driver_updated",
        "status_dispatched",
        "status_on_the_way",
        "status_arrived",
    }
)
_CONFIRM_LINK_KINDS = frozenset({"thank_you", "thank_you_wedding"}) | _TRIP_KINDS


def _keys_for(kind: str) -> set[str]:
    def group(name: str) -> set[str]:
        return {k for k, _ in _GROUPS[name]}

    if kind == "trip_confirm_affiliate":
        return (
            group("Trip") | group("Affiliate") | group("Company") | group("Driver") | {"ack_link"}
        )
    keys = group("Customer") | group("Order") | group("Payment") | group("Company")
    if kind not in _DRIVER_KINDS and kind != "review_request":
        keys |= {"quote_link", "pay_link"}
    if kind in _TRIP_KINDS:
        keys |= group("Trip")
    if kind in _DRIVER_KINDS:
        keys |= group("Driver")
    if kind in _CONFIRM_LINK_KINDS:
        keys.add("confirm_link")
    if kind == "review_request":
        keys.add("review_link")
    return keys


def fields_for_kind(kind: str) -> list[Field]:
    """The fields offered (and accepted) for a seeded kind, in registry order (spec B page)."""
    keys = _keys_for(kind)
    return [f for key, f in FIELDS.items() if key in keys]


def fields_for(trigger_key: str, audience: str, key: str = "") -> list[Field]:
    """The fields a message on this trigger and audience may use, in registry order.

    Only what the sender fills: the Affiliate group is for the affiliate audience; `confirm_link`
    only on trip-level triggers for a non-affiliate audience (`rules.fill_links`); `ack_link`,
    `review_link` and a staff report's values only for the seeded message that carries them,
    while it stays on its trigger (`key` names that seeded message).
    """
    trigger = triggers.get(trigger_key)
    groups = set(trigger.field_groups)
    if audience != "affiliate":
        groups.discard("Affiliate")
    keys = {k for g in groups for k, _ in _GROUPS[g]}
    keys -= set(_SENDER_LINKS)
    if "Links" in groups:
        keys |= {"quote_link", "pay_link"}
        if trigger.level == "trip" and audience != "affiliate":
            keys.add("confirm_link")
    if _original_trigger(key, trigger_key):
        keys |= required_for(key) | REPORT_FIELDS.get(key, frozenset())
    return [f for k, f in FIELDS.items() if k in keys]


def _original_trigger(key: str, trigger_key: str) -> bool:
    default = defaults.ORIGINALS.get(key)
    return default is not None and default.trigger == trigger_key


def _names(text: str) -> set[str]:
    """Field names used in a text, including inside format specs; escaped braces are literal.

    Raises ValueError if unbalanced."""
    names: set[str] = set()
    for _, name, spec, _ in string.Formatter().parse(text):
        if name is not None:
            names.add(name)
        if spec:
            names |= _names(spec)
    return names


def _has_formatting(text: str) -> bool:
    """Whether any field carries a format spec or conversion (`{name:x}`, `{name!r}`)."""
    return any(
        name is not None and (spec or conv)
        for _, name, spec, conv in string.Formatter().parse(text)
    )


def _default_texts(kind: str) -> tuple[str, ...]:
    d = defaults.ORIGINALS.get(kind)
    return (d.subject, d.email_body, d.sms_body) if d else ()


def required_for(kind: str) -> frozenset[str]:
    """Link keys the kind's default text uses on any channel; an edit must keep them."""
    names: set[str] = set()
    for text in _default_texts(kind):
        names |= _names(text)
    return frozenset(names & set(_LINKS))


def _channels(kind: str) -> tuple[str, ...]:
    d = defaults.ORIGINALS.get(kind)
    return ("email", "sms") if d is None else d.channels


_PURPOSE = {
    "pay_link": "the customer pays",
    "confirm_link": "the customer confirms",
    "ack_link": "the customer acknowledges",
    "quote_link": "the customer opens the quote",
    "review_link": "the customer leaves a review",
    "terms_link": "the customer accepts the terms",
    "account_button": "they set their password",
    "resume_button": "the couple updates their details",
    "report_table": "staff see what's in the report",
}
_CHANNEL_NAME = {"email": "email", "sms": "SMS"}


def _check_boxes(boxes: dict[str, str], allowed: set[str]) -> tuple[dict[str, str], set[str]]:
    """Per-box brace, unknown-field and formatting errors, and every field name used."""
    errors: dict[str, str] = {}
    present: set[str] = set()
    for box, text in boxes.items():
        try:
            names = _names(text)
        except ValueError:
            errors[box] = "Unmatched { or }. To show a brace, type {{ or }}."
            continue
        present |= names
        unknown = sorted(n for n in names if n not in allowed)
        if unknown:
            errors[box] = f"Unknown field {{{unknown[0]}}}. Use the buttons below to insert fields."
        elif _has_formatting(text):
            errors[box] = (
                "Formatting isn't supported inside a field. Use the buttons below to insert fields."
            )
    return errors, present


def _missing_link(
    errors: dict[str, str], present: set[str], required: frozenset[str], order: tuple[str, ...]
) -> None:
    """Report the first required link the wording lacks against the first channel's box
    (none while no channel is on)."""
    missing = sorted(required - present)
    if not missing or not order:
        return
    first = _BODY_BOX[order[0]]
    if first not in errors:
        key = missing[0]
        errors[first] = f"This message needs {{{key}}} — it's how {_PURPOSE[key]}."


_BOX_CHANNEL = {"subject": "email", "email_body": "email", "sms_body": "sms"}
_BODY_BOX = {"email": "email_body", "sms": "sms_body"}
_BOX_ORDER = ("subject", "email_body", "sms_body")


def validate(data, **texts: str) -> dict[str, str]:
    """Save-time errors for a message, keyed by form field; {} when valid.

    `validate(row_data)`: `row_data` holds trigger, audience, send_sms, send_email, subject,
    email_body, sms_body, recipients, staff_emails, timing_direction and, for a seeded message,
    key and seeded. `validate(kind, subject=, email_body=, sms_body=)` is spec B's text-only
    check, kept until the editor replaces that page.
    """
    if isinstance(data, str):
        return _validate_kind(data, **texts)
    trigger = triggers.CATALOG.get(data.get("trigger", ""))
    if trigger is None:
        return {"trigger": "Pick when this message goes out."}
    audience = data.get("audience", "customer")
    seeded = bool(data.get("seeded"))
    key = data.get("key", "") if seeded else ""
    builtin = _original_trigger(key, trigger.key)
    on = {"sms": bool(data.get("send_sms")), "email": bool(data.get("send_email"))}
    errors: dict[str, str] = {}
    if not any(on.values()):
        errors["channels"] = "Turn on Text or Email."
    if audience == "staff":
        if not data.get("staff_emails"):
            errors["staff_emails"] = "Add at least one email."
    elif not data.get("recipients"):
        errors["recipients"] = "Tick at least one person."
    if builtin and set_by_caller(key):
        errors.pop("recipients", None)
        errors.pop("staff_emails", None)
    if data.get("timing_direction") == "before" and trigger.kind != "date":
        errors["timing_direction"] = "Before only works with a date."

    boxes = {b: data.get(b, "") or "" for b in _BOX_ORDER}
    allowed = {f.key for f in fields_for(trigger.key, audience, key)}
    box_errors, present = _check_boxes(boxes, allowed)
    order = tuple(c for c in (_channels(key) if builtin else ("sms", "email")) if on[c])
    _missing_link(box_errors, present, required_for(key) if builtin else frozenset(), order)
    for box, text in boxes.items():
        if box in box_errors or text.strip() or not on[_BOX_CHANNEL[box]]:
            continue
        if box == "subject":
            box_errors[box] = "Add a subject for the email."
        else:
            name = _CHANNEL_NAME[_BOX_CHANNEL[box]]
            box_errors[box] = f"Can't be empty — this message sends by {name}."
    return errors | box_errors


def set_by_caller(key: str) -> bool:
    """A seeded invite or password reset: its caller names the person (the account holder),
    so the row lists no one of its own."""
    return key in defaults.INVITES and not defaults.INVITES[key].recipients


def wording_errors(data: dict) -> dict[str, str]:
    """The errors that stop a row's wording rendering at all (a stray brace, a field the
    trigger can't fill, formatting inside a field), keyed by box; {} when it renders.
    ``data`` is `validate`'s row data."""
    trigger = triggers.CATALOG.get(data.get("trigger", ""))
    if trigger is None:
        return {}
    key = data.get("key", "") if data.get("seeded") else ""
    allowed = {f.key for f in fields_for(trigger.key, data.get("audience", "customer"), key)}
    boxes = {b: data.get(b, "") or "" for b in _BOX_ORDER}
    return _check_boxes(boxes, allowed)[0]


def _validate_kind(kind: str, *, subject: str, email_body: str, sms_body: str) -> dict[str, str]:
    """Per-box errors for a seeded kind's draft; {} when valid (spec B page)."""
    boxes = dict(zip(_BOX_ORDER, (subject, email_body, sms_body), strict=True))
    channels = _channels(kind)
    errors, present = _check_boxes(boxes, {f.key for f in fields_for_kind(kind)})
    _missing_link(errors, present, required_for(kind), channels)
    for box, text in boxes.items():
        if box not in errors and not text.strip() and _BOX_CHANNEL[box] in channels:
            name = _CHANNEL_NAME[_BOX_CHANNEL[box]]
            errors[box] = f"Can't be empty — this message sends by {name}."
    return errors
