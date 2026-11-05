"""The original of every seeded message, and the seeding that writes them as rows.

The wording is not repeated here: each default points at the client copy in
`touchpoint_templates`. `seed` creates the rows that are missing (folding in what spec B saved
for a key) and never overwrites a seeded row; `reset` copies an original back over its row.

`seed` runs inside migrations with the historical app registry, so this module must not import
live models at load time.
"""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import timedelta

from .touchpoint_templates import TEMPLATES, THANK_YOU_WEDDING, TouchPointTemplate

# A copy of `apps.public.services.WEDDING_SERVICE_NAME` (a test keeps them equal), so the
# migration doesn't import the public site's services.
WEDDING_SERVICE_NAME = "Wedding Transportation"

TIMING_UNITS = ("minutes", "hours", "days")


@dataclass(frozen=True)
class MessageDefault:
    key: str
    name: str
    section: str
    audience: str
    trigger: str
    timing_amount: int
    timing_unit: str
    timing_direction: str
    send_sms: bool
    send_email: bool
    subject: str
    email_body: str
    sms_body: str
    recipients: tuple[str, ...]
    staff_emails: tuple[str, ...]
    enabled: bool
    service_type_names: tuple[str, ...] = ()

    @property
    def channels(self) -> tuple[str, ...]:
        """The channels it sends on, in the order the original lists them (the first is the
        one a missing link is reported against)."""
        on = {"sms": self.send_sms, "email": self.send_email}
        return tuple(c for c in _template(self.key).channels if on[c])


_BOOKING = ("booking_contact",)
_DRIVER_PEOPLE = ("booking_contact", "role:day_of_coordinator", "role:transportation")
_WEDDING = (WEDDING_SERVICE_NAME,)

# key: (name, section, trigger, (amount, unit), recipients, service types, enabled)
# Names are today's labels on the Messages page; the timing restates each template's offset
# in the units the routing table uses (a test holds them equal).
_ROWS: dict[str, tuple] = {
    "tp1_welcome": ("Welcome message", "leads_quotes", "lead_created", (30, "minutes"), _BOOKING),
    "tp2_lead_followup": ("Lead follow-up", "leads_quotes", "lead_created", (2, "hours"), _BOOKING),
    "tp3_quote_sent_sms": ("Quote sent", "leads_quotes", "quote_sent", (3, "minutes"), _BOOKING),
    "tp4_viewed_sms": (
        "Quote viewed (text)",
        "leads_quotes",
        "quote_viewed",
        (20, "minutes"),
        _BOOKING,
    ),
    "tp5_viewed_email": (
        "Quote viewed (email)",
        "leads_quotes",
        "quote_viewed",
        (2, "hours"),
        _BOOKING,
    ),
    "tp6_quote_followup": (
        "Quote follow-up",
        "leads_quotes",
        "quote_sent",
        (24, "hours"),
        _BOOKING,
    ),
    "tp7_expiring": ("Quote expiring", "leads_quotes", "quote_expiry", (24, "hours"), _BOOKING),
    "tp8_expired": ("Quote expired", "leads_quotes", "quote_expiry", (24, "hours"), _BOOKING),
    "payment_reminder": (
        "Payment reminder",
        "booking_payments",
        "balance_due",
        (72, "hours"),
        _BOOKING,
    ),
    "order_confirmed": (
        "Order confirmed",
        "booking_payments",
        "order_confirmed",
        (0, "minutes"),
        _BOOKING,
    ),
    "order_cancelled": (
        "Order cancelled",
        "booking_payments",
        "order_cancelled",
        (0, "minutes"),
        _BOOKING,
    ),
    "order_auth_expired": (
        "Deposit hold released",
        "booking_payments",
        "deposit_hold_released",
        (0, "minutes"),
        _BOOKING,
    ),
    "wed_final_details": (
        "Wedding details request",
        "trip_day",
        "pickup",
        (7, "days"),
        ("booking_contact", "role:couple", "role:lead_planner"),
        _WEDDING,
    ),
    "trip_confirm_customer": ("Trip confirmation", "trip_day", "pickup", (72, "hours"), _BOOKING),
    "trip_confirm_customer_2": (
        "Trip confirmation — second notice",
        "trip_day",
        "pickup",
        (48, "hours"),
        _BOOKING,
    ),
    "trip_confirm_affiliate": (
        "Affiliate trip confirmation",
        "affiliates",
        "pickup",
        (48, "hours"),
        ("company",),
    ),
    "driver_released": (
        "Driver details",
        "trip_day",
        "pickup_dispatched",
        (12, "hours"),
        _DRIVER_PEOPLE,
    ),
    "driver_updated": (
        "Driver details updated",
        "trip_day",
        "driver_changed",
        (0, "minutes"),
        _DRIVER_PEOPLE,
    ),
    # Not on today's Messages page; named like its siblings "On the way" and "Arrived".
    "status_dispatched": (
        "Dispatched",
        "trip_day",
        "status_dispatched",
        (0, "minutes"),
        _BOOKING,
        (),
        False,
    ),
    "status_on_the_way": (
        "On the way",
        "trip_day",
        "status_on_the_way",
        (0, "minutes"),
        _BOOKING,
        (),
        False,
    ),
    "status_arrived": (
        "Arrived",
        "trip_day",
        "status_arrived",
        (0, "minutes"),
        _BOOKING,
        (),
        False,
    ),
    "thank_you": ("Thank-you", "after_trip", "all_trips_done", (2, "hours"), _BOOKING),
    "thank_you_wedding": (
        "Thank-you (wedding)",
        "after_trip",
        "all_trips_done",
        (2, "hours"),
        _BOOKING,
        _WEDDING,
    ),
    "review_request": ("Review request", "after_trip", "all_trips_done", (1, "days"), _BOOKING),
}


def _template(key: str) -> TouchPointTemplate:
    return THANK_YOU_WEDDING if key == "thank_you_wedding" else TEMPLATES[key]


def _build(key: str, row: tuple) -> MessageDefault:
    name, section, trigger, (amount, unit), recipients, *rest = row
    service_types = rest[0] if rest else ()
    enabled = rest[1] if len(rest) > 1 else True
    base = _template(key)
    return MessageDefault(
        key=key,
        name=name,
        section=section,
        audience=base.audience,
        trigger=trigger,
        timing_amount=amount,
        timing_unit=unit,
        timing_direction="before" if base.offset < timedelta(0) else "after",
        send_sms="sms" in base.channels,
        send_email="email" in base.channels,
        subject=base.subject,
        email_body=base.email_body,
        sms_body=base.sms_body,
        recipients=tuple(recipients),
        staff_emails=(),
        enabled=enabled,
        service_type_names=tuple(service_types),
    )


DEFAULTS: dict[str, MessageDefault] = {key: _build(key, row) for key, row in _ROWS.items()}

# The fields `reset` copies back. The on/off switch is not part of the original: resetting the
# wording never turns a message on or off.
_RESET_FIELDS = tuple(
    f.name for f in fields(MessageDefault) if f.name not in ("key", "enabled", "service_type_names")
)

# Spec B's shared switches (`overrides._SWITCH_OF`): these rows follow another row's on/off
# today, and the wedding thank-you its timing too, so each seeds from that row.
_SWITCH_OF = {"trip_confirm_customer_2": "trip_confirm_customer", "thank_you_wedding": "thank_you"}
_TIMED_BY = {"thank_you_wedding": "thank_you"}
_TEXTS = ("subject", "email_body", "sms_body")


def _models(apps):
    if apps is None:
        from django.apps import apps as live

        apps = live
    return (
        apps.get_model("messaging", "MessageTemplate"),
        apps.get_model("messaging", "TouchPoint"),
        apps.get_model("leads", "ServiceType"),
    )


def _service_types(ServiceType, names: tuple[str, ...]) -> list:
    """The service types a default filters on, by name ignoring case; a missing one is
    created inactive so the filter has something to point at."""
    found = []
    for name in names:
        match = ServiceType.objects.filter(name__iexact=name).order_by("pk").first()
        found.append(match or ServiceType.objects.create(name=name, active=False))
    return found


def _timing(saved, key: str) -> tuple[int, str]:
    """Spec B's saved timing for a key when it set one, else the default.

    Spec B never applied a timing to a message that sends when it happens, so a stray one
    there is ignored here too."""
    default = DEFAULTS[key]
    if (
        saved is not None
        and _template(key).anchor != "triggered"
        and saved.timing_amount is not None
        and saved.timing_unit in TIMING_UNITS
    ):
        return saved.timing_amount, saved.timing_unit
    return default.timing_amount, default.timing_unit


def _values(key: str, saved: dict) -> dict:
    """The full row for a key: the default with spec B's saved edits folded in."""
    default = DEFAULTS[key]
    row = saved.get(key)
    values = {name: getattr(default, name) for name in _RESET_FIELDS}
    values["recipients"] = list(default.recipients)
    values["staff_emails"] = list(default.staff_emails)
    if row is not None:
        values.update({name: getattr(row, name) for name in _TEXTS if getattr(row, name)})
    timed_by = _TIMED_BY.get(key, key)
    values["timing_amount"], values["timing_unit"] = _timing(saved.get(timed_by), timed_by)
    switch = _SWITCH_OF.get(key, key)
    owner = saved.get(switch)
    values["enabled"] = owner.enabled if owner is not None else DEFAULTS[switch].enabled
    values["seeded"] = True
    return values


def seed(apps=None, keys: Iterable[str] | None = None) -> int:
    """Create every missing seeded row; returns how many were written.

    A row spec B saved for a key (not yet seeded) is completed in place: its non-blank
    wording, its timing and its switch win over the default. A seeded row is never touched.
    Then every TouchPoint without a message is pointed at the row for its kind.
    ``apps`` is a migration's app registry; None uses the live models. ``keys`` limits the
    run to those messages (a migration passes the frozen list it owns); None means all."""
    MessageTemplate, TouchPoint, ServiceType = _models(apps)
    keys = tuple(DEFAULTS if keys is None else keys)
    existing = {row.key: row for row in MessageTemplate.objects.filter(key__in=DEFAULTS)}
    saved = {key: row for key, row in existing.items() if not row.seeded}
    written = 0
    for key in keys:
        default = DEFAULTS[key]
        row = existing.get(key)
        if row is not None and row.seeded:
            continue
        values = _values(key, saved)
        if row is None:
            row = MessageTemplate(key=key)
        for name, value in values.items():
            setattr(row, name, value)
        row.save()
        row.service_types.set(_service_types(ServiceType, default.service_type_names))
        written += 1
    for row in MessageTemplate.objects.filter(key__in=keys):
        TouchPoint.objects.filter(kind=row.key, message__isnull=True).update(message=row)
    return written


def reset(row):
    """Copy the original back over a seeded row (all but its on/off switch); returns it."""
    default = DEFAULTS[row.key]
    for name in _RESET_FIELDS:
        setattr(row, name, getattr(default, name))
    row.recipients = list(default.recipients)
    row.staff_emails = list(default.staff_emails)
    row.save()
    _, _, ServiceType = _models(None)
    row.service_types.set(_service_types(ServiceType, default.service_type_names))
    return row
