"""The original of every seeded message, and the seeding that writes them as rows.

`DEFAULTS` are the touch-point messages: their wording is not repeated here, each points at
the client copy in `touchpoint_templates`. `CLICKS` are the staff-click sends (spec C section
8): their wording is today's button text, kept here, with the parts the caller generates
(the quote summary, the receipt lines, a trip sheet, a button) as block placeholders.
`INVITES` are the invites, the planner password reset and the wedding-details email, the
same way. `ORIGINALS` is all three. `seed` creates the rows that are missing (folding in
what spec B saved for a key) and never overwrites a seeded row; `reset` copies an original
back over its row.

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
        one a missing link is reported against). A staff click lists the text first."""
        on = {"sms": self.send_sms, "email": self.send_email}
        order = _template(self.key).channels if self.key in _ROWS else ("sms", "email")
        return tuple(c for c in order if on[c])


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


# --- staff clicks ----------------------------------------------------------------------
#
# Today's button sends, word for word (`test_staff_click_golden` holds them). A block
# placeholder alone on its line (`{quote_summary}`, `{button}`, `{receipt_lines}`,
# `{trip_sheet}`, `{cancelled_trip_sheet}`) is a part its caller generates; the other
# click-only fields (`fields._GROUPS`) are values its caller hands over.

_QUOTE_EMAIL = (
    "Hi {contact_name},\n\n"
    "Your quote is ready. Review the full itinerary and reserve your date below.\n\n"
    "{quote_summary}\n\n"
    "{button}\n\n"
    "This quote is valid through {quote_valid_through}.\n\n"
    "Questions? Reply to this email or call {company_phone}.\n\n"
    "--\n"
    "{company_name}\n"
    "Luxury charter & chauffeur service\n"
    "{company_email} · {company_phone}"
)
_PAY_LINK = "Here's a secure link to pay for {payment_for}: {pay_link}"
_TERMS_LINK = "Please review and accept the terms for reservation {quote_no}: {terms_link}"


def _click(
    key: str,
    name: str,
    section: str,
    *,
    trigger: str = "",
    audience: str = "customer",
    channels: tuple[str, ...] = ("sms", "email"),
    subject: str = "",
    email_body: str = "",
    sms_body: str = "",
    recipients: tuple[str, ...] = _BOOKING,
) -> MessageDefault:
    return MessageDefault(
        key=key,
        name=name,
        section=section,
        audience=audience,
        trigger=trigger or key,
        timing_amount=0,
        timing_unit="minutes",
        timing_direction="after",
        send_sms="sms" in channels,
        send_email="email" in channels,
        subject=subject,
        email_body=email_body,
        sms_body=sms_body,
        recipients=recipients,
        staff_emails=(),
        enabled=True,
    )


CLICKS: dict[str, MessageDefault] = {
    d.key: d
    for d in (
        _click(
            "send_quote",
            "Quote",
            "leads_quotes",
            subject="Your {company_name} quote {quote_no}",
            email_body=_QUOTE_EMAIL,
            sms_body=(
                "Hi {contact_name}, here's your All Pro Charter quote {quote_no} for "
                "{quote_total}. To confirm, pay your {deposit_pct}% deposit of "
                "{deposit_amount} here: {quote_link}"
            ),
        ),
        # APC-105 draft wording, until the client signs it off: no deposit is asked.
        _click(
            "send_quote_on_account",
            "Quote (billed to an account)",
            "leads_quotes",
            trigger="send_quote",
            subject="Your {company_name} quote {quote_no}",
            email_body=_QUOTE_EMAIL,
            sms_body=(
                "Hi {contact_name}, here's your All Pro Charter quote {quote_no} for "
                "{quote_total}, billed to your account. Review and book here: {quote_link}"
            ),
        ),
        # A Podium message (text, else email), which has no subject line.
        _click(
            "send_pay_link",
            "Payment link",
            "booking_payments",
            email_body=_PAY_LINK,
            sms_body=_PAY_LINK,
        ),
        # CLIENT COPY PENDING (APC-27): a placeholder sentence until the wording is signed off.
        _click(
            "send_terms_link",
            "Terms link",
            "booking_payments",
            email_body=_TERMS_LINK,
            sms_body=_TERMS_LINK,
        ),
        # APC-103 draft copy until the client signs it off.
        _click(
            "payment_receipt",
            "Receipt",
            "booking_payments",
            channels=("email",),
            subject="Your {company_name} {receipt_name}",
            email_body=(
                "Hi {first_name},\n\n"
                "{receipt_intro} Here are the details.\n\n"
                "{receipt_lines}\n\n"
                "{receipt_explanation}\n"
                "Questions about this {receipt_kind}? Reply to this email or call "
                "{company_phone}.\n\n"
                "--\n"
                "{company_name}\n"
                "{company_email} · {company_phone}"
            ),
            recipients=("billing_contact",),
        ),
        _click(
            "affiliate_offer",
            "Trip offer",
            "affiliates",
            audience="affiliate",
            channels=("email",),
            subject="Trip offer — {trip_day}",
            email_body=(
                "{vendor_name} — trip offer from All Pro Charter\n\n"
                "{trip_sheet}\n\n"
                "Payout offered: {payout}\n\n"
                "Reply to this email or call the office to accept."
            ),
            recipients=("company",),
        ),
        _click(
            "affiliate_cancel",
            "Trip cancelled",
            "affiliates",
            audience="affiliate",
            channels=("email",),
            subject="Trip cancelled — {trip_day}",
            email_body=(
                "{vendor_name} — trip cancelled\n\n"
                "Thank you for covering this trip. Our plans have changed and we have "
                "released it, so no further action is required on your side.\n\n"
                "We appreciate your time and look forward to working with you again soon.\n\n"
                "{cancelled_trip_sheet}\n\n"
                "Questions? Reply to this email or call {company_phone}.\n\n"
                "--\n"
                "{company_name}"
            ),
            recipients=("company",),
        ),
    )
}

# --- invites, the planner password reset, the wedding-details email -----------------------
#
# Today's emails, word for word (`test_invites_golden` holds them). Each caller names the
# person and hands over the link button (`{account_button}`, `{resume_button}`: in plain
# text the link itself) and, for the wedding email, every answer the couple gave
# (`{wedding_details}`). The invites and the reset name no one of their own: they go to the
# account holder (`fields.set_by_caller`).

_SIGNATURE = "--\n{company_name}\n{company_email} · {company_phone}"
_SIGNATURE_WITH_LINE = (
    "--\n{company_name}\nLuxury charter & chauffeur service\n{company_email} · {company_phone}"
)

INVITES: dict[str, MessageDefault] = {
    d.key: d
    for d in (
        _click(
            "planner_invite",
            "Planner invite",
            "planners",
            audience="planner",
            channels=("email",),
            subject="Your wedding transportation with {company_name}",
            email_body=(
                "Hi {account_name},\n\n"
                "{company_name} has set up a page for the weddings you're planning with us: "
                "every pickup, vehicle and, closer to the day, every driver, in one place.\n\n"
                "Create your account here:\n"
                "{account_button}\n\n"
                "This link works once and expires in 7 days. If it lapses, ask your "
                "coordinator to send a new one.\n\n"
                "If you weren't expecting this, you can ignore this email.\n\n" + _SIGNATURE
            ),
            recipients=(),
        ),
        _click(
            "planner_password_reset",
            "Planner password reset",
            "planners",
            audience="planner",
            channels=("email",),
            subject="Reset your {company_name} planner password",
            email_body=(
                "Hi {account_name},\n\n"
                "Someone asked to reset the password for your {company_name} planner account.\n"
                "Choose a new one here:\n"
                "{account_button}\n\n"
                "This link works once and expires in 7 days. If you didn't ask for this, you "
                "can ignore this email; your password stays as it is.\n\n" + _SIGNATURE
            ),
            recipients=(),
        ),
        _click(
            "user_invite",
            "Staff user invite",
            "staff_reports",
            audience="staff",
            channels=("email",),
            subject="You've been invited to the {company_name} Lead Manager",
            email_body=(
                "Hi {account_first_name},\n\n"
                "You've been added to the {company_name} Lead Manager as {role_label}.\n"
                "{access_level}\n\n"
                "Set your password to activate your account:\n"
                "{account_button}\n\n"
                "This link expires in 7 days. If it lapses, ask an admin to resend your "
                "invite.\n\n"
                "If you weren't expecting this, you can ignore this email — the account stays "
                "locked until someone uses the link above.\n\n" + _SIGNATURE_WITH_LINE
            ),
            recipients=(),
        ),
        _click(
            "wedding_details_received",
            "Wedding details received",
            "leads_quotes",
            channels=("email",),
            subject="We have your wedding details · {quote_no}",
            email_body=(
                "Hi {form_name},\n\n"
                "Thank you — we have your wedding details, and one of our wedding "
                "professionals is building your day from them: every pickup, every vehicle. "
                "You'll have it, with a full quote, within one business day. No deposit, "
                "nothing locked in.\n\n"
                "{wedding_details}\n\n"
                "Plans still firming up? Open your details and change the guest count, the "
                "hotels or the times — we'll build from whatever is current:\n"
                "{resume_button}\n\n"
                "Questions? Reply to this email or call {company_phone}.\n\n" + _SIGNATURE_WITH_LINE
            ),
        ),
    )
}

# Every seeded message's original, by key.
ORIGINALS: dict[str, MessageDefault] = {**DEFAULTS, **CLICKS, **INVITES}

# The fields `reset` copies back. The on/off switch is not part of the original: resetting the
# wording never turns a message on or off.
_RESET_FIELDS = tuple(
    f.name for f in fields(MessageDefault) if f.name not in ("key", "enabled", "service_type_names")
)

# Spec B's shared switches (its `_SWITCH_OF`): these rows follow another row's on/off
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

    Spec B never applied a timing to a message that sends when it happens (nor knew the
    staff clicks), so a stray one there is ignored here too."""
    default = ORIGINALS[key]
    if (
        saved is not None
        and key in _ROWS
        and _template(key).anchor != "triggered"
        and saved.timing_amount is not None
        and saved.timing_unit in TIMING_UNITS
    ):
        return saved.timing_amount, saved.timing_unit
    return default.timing_amount, default.timing_unit


def _values(key: str, saved: dict) -> dict:
    """The full row for a key: the default with spec B's saved edits folded in."""
    default = ORIGINALS[key]
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
    values["enabled"] = owner.enabled if owner is not None else ORIGINALS[switch].enabled
    values["seeded"] = True
    return values


def seed(apps=None, keys: Iterable[str] | None = None) -> int:
    """Create every missing seeded row; returns how many were written.

    A row spec B saved for a key (not yet seeded) is completed in place: its non-blank
    wording, its timing and its switch win over the default. A seeded row is never touched.
    Then every TouchPoint without a message is pointed at the row for its kind.
    ``apps`` is a migration's app registry; None uses the live models. ``keys`` limits the
    run to those messages (a migration passes the frozen list it owns); None means every
    touch-point message (`DEFAULTS`). The staff clicks are seeded by their own migration, by
    key."""
    MessageTemplate, TouchPoint, ServiceType = _models(apps)
    keys = tuple(DEFAULTS if keys is None else keys)
    existing = {row.key: row for row in MessageTemplate.objects.filter(key__in=ORIGINALS)}
    saved = {key: row for key, row in existing.items() if not row.seeded}
    written = 0
    for key in keys:
        default = ORIGINALS[key]
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
    default = ORIGINALS[row.key]
    for name in _RESET_FIELDS:
        setattr(row, name, getattr(default, name))
    row.recipients = list(default.recipients)
    row.staff_emails = list(default.staff_emails)
    row.save()
    _, _, ServiceType = _models(None)
    row.service_types.set(_service_types(ServiceType, default.service_type_names))
    return row
