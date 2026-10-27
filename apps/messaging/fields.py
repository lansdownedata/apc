"""The fields an owner may put in a message, per kind, and the save-time check on a draft."""

import string
from dataclasses import dataclass

from . import overrides


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
        ("trip_routing", "Route"),
        ("trip_passengers", "Passengers"),
        ("trip_vehicle", "Vehicle type"),
        ("trip_sheet", "Trip details"),
        ("contact_on_file", "Day-of contact"),
    ],
    "Driver": [
        ("driver_name", "Driver name"),
        ("driver_cell", "Driver cell"),
        ("vehicle_description", "Vehicle"),
        ("vehicle_number", "Vehicle number"),
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
}

FIELDS: dict[str, Field] = {
    key: Field(key, label, group) for group, items in _GROUPS.items() for key, label in items
}

_LINKS = ("quote_link", "pay_link", "confirm_link", "ack_link", "review_link")
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
    group = lambda name: {k for k, _ in _GROUPS[name]}  # noqa: E731
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


def fields_for(kind: str) -> list[Field]:
    """The fields offered (and accepted) for a kind, in registry order."""
    keys = _keys_for(kind)
    return [f for key, f in FIELDS.items() if key in keys]


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
    try:
        t = overrides.default(kind)
    except KeyError:
        return ()
    return (t.subject, t.email_body, t.sms_body)


def required_for(kind: str) -> frozenset[str]:
    """Link keys the kind's default text uses on any channel; an edit must keep them."""
    names: set[str] = set()
    for text in _default_texts(kind):
        names |= _names(text)
    return frozenset(names & set(_LINKS))


def _channels(kind: str) -> tuple[str, ...]:
    try:
        return overrides.default(kind).channels
    except KeyError:
        return ("email", "sms")


_PURPOSE = {
    "pay_link": "pays",
    "confirm_link": "confirms",
    "ack_link": "acknowledges",
    "quote_link": "opens the quote",
    "review_link": "leaves a review",
}
_CHANNEL_NAME = {"email": "email", "sms": "SMS"}


def validate(kind: str, *, subject: str, email_body: str, sms_body: str) -> dict[str, str]:
    """Per-box errors for a draft, keyed subject | email_body | sms_body; {} when valid.

    One message per box, in priority order: braces, unknown field, missing link, empty.
    """
    boxes = {"subject": subject, "email_body": email_body, "sms_body": sms_body}
    allowed = {f.key for f in fields_for(kind)}
    channels = _channels(kind)
    used = {"subject": "email", "email_body": "email", "sms_body": "sms"}
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

    missing = sorted(required_for(kind) - present)
    if missing:
        first = next(
            (b for b in ("subject", "email_body", "sms_body") if used[b] == channels[0]), None
        )
        first = "email_body" if first == "subject" else first
        if first and first not in errors:
            key = missing[0]
            errors[first] = f"This message needs {{{key}}} — it's how the customer {_PURPOSE[key]}."
    for box, text in boxes.items():
        if box not in errors and not text.strip() and used[box] in channels:
            name = _CHANNEL_NAME[used[box]]
            errors[box] = f"Can't be empty — this message sends by {name}."
    return errors
