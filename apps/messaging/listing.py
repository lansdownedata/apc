"""What the Messages page shows: rows grouped into sections, filtered, with their problems.

Pure reading. `build_sections` takes the live rows (service types prefetched) and the page's
query-string filters; `summary` and `problems` describe one row.
"""

from dataclasses import dataclass, field

from apps.leads.models import LeadContact

from . import defaults, fields, triggers
from .models import MessageTemplate

NO_RECIPIENTS = "No recipients ticked, so it won't send"
BAD_FIELD = "Uses a field this trigger can't fill"
MASTER_OFF = "On, but all messaging is off"

_RECIPIENT_LABEL = {
    "passengers": "Passengers",
    "booking_contact": "Booking contact",
    "billing_contact": "Billing contact",
    "company": "Company",
    "driver": "Driver",
    "planner": "Planner",
}
_ROLE_LABEL = dict(LeadContact.Role.choices)
_CHANNEL_WORDS = {(True, False): "Text", (False, True): "Email", (True, True): "Text and Email"}
FILTER_KEYS = ("q", "audience", "channel", "trigger", "service", "state", "origin")


@dataclass
class Item:
    """One row as the page shows it."""

    row: MessageTemplate
    summary: str
    problems: list[str]
    tag: str
    click_label: str

    @property
    def name(self) -> str:
        return self.row.name

    @property
    def has_switch(self) -> bool:
        return not self.click_label


@dataclass
class Section:
    key: str
    label: str
    rows: list[Item] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def attention(self) -> int:
        return sum(1 for item in self.rows if item.problems)


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _timing(row: MessageTemplate) -> str:
    trigger = triggers.CATALOG.get(row.trigger)
    if trigger is None:
        return row.trigger
    if trigger.kind == "staff_click":
        return "Staff click"
    if trigger.kind == "schedule":
        return f"On schedule ({_lower_first(trigger.label)})"
    what = _lower_first(trigger.label)
    amount = row.timing_amount or 0
    if not amount:
        return f"At {what}" if trigger.kind == "date" else f"Right after {what}"
    unit = (row.timing_unit or "minutes").removesuffix("s")
    word = unit if amount == 1 else f"{unit}s"
    side = "before" if row.timing_direction == "before" else "after"
    return f"{amount} {word} {side} {what}"


def _recipients(row: MessageTemplate) -> str:
    if row.audience == MessageTemplate.Audience.STAFF:
        count = len(row.staff_emails or [])
        return f"{count} staff email{'' if count == 1 else 's'}" if count else "No staff emails"
    names = []
    for key in row.recipients or []:
        kind, _, arg = key.partition(":")
        label = _ROLE_LABEL.get(arg) if kind == "role" else _RECIPIENT_LABEL.get(key)
        if label:
            names.append(label)
    return ", ".join(names) or "No one"


def summary(row: MessageTemplate) -> str:
    """e.g. "3 hours before pickup, once dispatched · Text · Passengers, Day-of coordinator"."""
    parts = [
        _timing(row),
        _CHANNEL_WORDS.get((row.send_sms, row.send_email), "No channel"),
        _recipients(row),
    ]
    types = sorted(t.name for t in row.service_types.all())
    if types:
        parts.append(", ".join(types))
    return " · ".join(parts)


def is_staff_click(row: MessageTemplate) -> bool:
    trigger = triggers.CATALOG.get(row.trigger)
    return trigger is not None and trigger.kind == "staff_click"


def problems(row: MessageTemplate, *, master_on: bool = True) -> list[str]:
    """What stops a row working as it reads, in the order the page lists them."""
    found = []
    staff = row.audience == MessageTemplate.Audience.STAFF
    if not (row.staff_emails if staff else row.recipients):
        found.append(NO_RECIPIENTS)
    if row.trigger in triggers.CATALOG:
        errors = fields.validate(
            {
                "trigger": row.trigger,
                "audience": row.audience,
                "send_sms": row.send_sms,
                "send_email": row.send_email,
                "subject": row.subject,
                "email_body": row.email_body,
                "sms_body": row.sms_body,
                "recipients": row.recipients,
                "staff_emails": row.staff_emails,
                "timing_direction": row.timing_direction,
                "key": row.key,
                "seeded": row.seeded,
            }
        )
        if any(msg.startswith("Unknown field") for msg in errors.values()):
            found.append(BAD_FIELD)
    if row.enabled and not master_on and not is_staff_click(row):
        found.append(MASTER_OFF)
    return found


def _edited(row: MessageTemplate) -> bool:
    default = defaults.DEFAULTS.get(row.key)
    if default is None:
        return False
    plain = (n for n in defaults._RESET_FIELDS if n not in ("recipients", "staff_emails"))
    if any(getattr(row, name) != getattr(default, name) for name in plain):
        return True
    if sorted(row.recipients or []) != sorted(default.recipients):
        return True
    if sorted(row.staff_emails or []) != sorted(default.staff_emails):
        return True
    return sorted(t.name for t in row.service_types.all()) != sorted(default.service_type_names)


def _tag(row: MessageTemplate) -> str:
    if not row.seeded:
        return "Added"
    return "Edited" if _edited(row) else ""


def _click_label(row: MessageTemplate) -> str:
    return triggers.CATALOG[row.trigger].label if is_staff_click(row) else ""


def _matches(item: Item, filters: dict) -> bool:
    row = item.row
    q = (filters.get("q") or "").strip().lower()
    if q and q not in f"{row.name} {item.summary}".lower():
        return False
    if filters.get("audience") and row.audience != filters["audience"]:
        return False
    channel = filters.get("channel")
    if channel == "sms" and not row.send_sms:
        return False
    if channel == "email" and not row.send_email:
        return False
    if channel == "both" and not (row.send_sms and row.send_email):
        return False
    if filters.get("trigger") and row.trigger != filters["trigger"]:
        return False
    service = filters.get("service")
    if service:
        ids = {str(t.pk) for t in row.service_types.all()}
        if ids and service not in ids:
            return False
    state = filters.get("state")
    if (state == "on" and not row.enabled) or (state == "off" and row.enabled):
        return False
    origin = filters.get("origin")
    return not ((origin == "seeded" and not row.seeded) or (origin == "added" and row.seeded))


def build_sections(rows, filters: dict, *, master_on: bool = True) -> list[Section]:
    """The seven sections in fixed order; empty ones are dropped while any filter is set."""
    sections = {s.value: Section(s.value, s.label) for s in MessageTemplate.Section}
    for row in rows:
        item = Item(
            row=row,
            summary=summary(row),
            problems=problems(row, master_on=master_on),
            tag=_tag(row),
            click_label=_click_label(row),
        )
        if _matches(item, filters):
            sections[row.section].rows.append(item)
    for section in sections.values():
        section.rows.sort(key=lambda item: (not item.problems, item.name.lower()))
    filtering = any(filters.get(key) for key in FILTER_KEYS)
    return [s for s in sections.values() if s.rows or not filtering]
