"""How audit entries read in the History drawer (APC-140, spec §6.2, §6.3).

Pure functions over stored entries: auditlog keeps every value as a string (`"None"` for
None, naive-UTC datetimes, pks for foreign keys), so each value is parsed by its field's
type and shown in the panel's zone. The only queries are `resolve_fks`, one `in_bulk` per
related model for the whole page.
"""

from __future__ import annotations

import re
import string
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.utils import dateformat, timezone
from django.utils.text import capfirst

from apps.audit import registry

if TYPE_CHECKING:
    from .models import AuditEntry

DASH = "—"
BLANK = (None, "", "None")
LONG_TEXT = 80
CREATE, UPDATE, DELETE = 0, 1, 2

WEBHOOKS = {
    "stripe": "Stripe",
    "gnet": "GNet",
    "podium": "Podium",
    "limoanywhere": "LimoAnywhere",
    "calendly": "Calendly",
}
AUTOMATIC_PREFIXES = ("cron:", "webhook:", "command:")

# The panels' own subjects: their lines need no name in front. Every other row (a charge,
# a vendor bill, a phone) names itself before the field: "Trip charge $450.00 · Status".
ANCHORS = frozenset(
    {
        "reservations.reservation",
        "leads.lead",
        "contacts.contact",
        "contacts.company",
        "vendors.vendor",
    }
)
CREATED = {"reservations.reservation": "Created the trip", "leads.lead": "Created the order"}
NOUNS = {"reservations.tripextra": "extra "}

ROUTE_KEY = re.compile(r"route\.(\d+)(?:\.(\w+))?")
ROUTE_FIELDS = (
    "address",
    "name",
    "note",
    "scheduled_time",
    "airport",
    "airline",
    "flight_number",
    "flight_direction",
)
ROUTE_LABELS = {"scheduled_time": "time"}
# Already their human `str()` when stored (children.py), so never resolved as FKs.
ROUTE_TEXT = frozenset({"airport", "airline"})

ADDRESS_SLOTS = {
    "primary_address": "Primary address",
    "billing_address": "Billing address",
    "address": "Address",
}
ADDRESS_FIELDS = {
    "landmark_name": "Place name",
    "line1": "Street",
    "line2": "Street line 2",
    "city": "City",
    "state": "State",
    "postal": "ZIP",
    "country": "Country",
}
ADDRESS_KEY = re.compile(rf"({'|'.join(ADDRESS_SLOTS)})\.(\w+)")


@dataclass
class Line:
    """One row of a group. A sentence line renders just `sentence`; a `long` one renders
    "<label> edited" with a Show toggle revealing old and new."""

    label: str
    old: str
    new: str
    sentence: str = ""
    long: bool = False
    prefix: str = ""


@dataclass
class Group:
    """Entries written together (one `cid`), newest first in the panel."""

    who: str
    when: datetime
    zone: str
    automatic: bool
    lines: list[Line]
    day: date

    @property
    def clock(self) -> str:
        """`3:12 PM EST`: the group's time in its zone, abbreviation always shown."""
        local = self.when.astimezone(_zone(self.zone))
        return f"{dateformat.format(local, 'g:i A')} {local.tzname()}"


def money(value: Any) -> str:
    """`1250` -> `$1,250.00`, `-125` -> `−$125.00` (a true minus sign); blank -> `$0.00`."""
    amount = Decimal(str(value or 0))
    sign = "−" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


# --- who ---


def who(entry: AuditEntry) -> str:
    """The person (full name, else email), or the source in words when no one signed in."""
    source = entry.source or ""
    name = ""
    if entry.actor_id:
        name = entry.actor.get_full_name() or entry.actor_email or entry.actor.email
    if source.startswith("import: "):
        upload = source.removeprefix("import: ")
        name = name or entry.actor_email
        return f"{name} via Excel import ({upload})" if name else f"Excel import ({upload})"
    return name or _source_words(source, entry.actor_email)


def _source_words(source: str, email: str) -> str:
    kind, _, rest = source.partition(": ")
    if kind == "webhook":
        return WEBHOOKS.get(rest, capfirst(rest))
    if kind in ("customer", "affiliate"):
        return f"{kind.capitalize()} · {rest}"
    if kind == "cron":
        return f"Scheduled job · {rest}"
    if kind == "command":
        return f"Command · {rest}"
    if source == "planner portal":
        return f"Planner · {email}" if email else "Planner"
    return email or capfirst(source) or "System"


def is_automatic(entry: AuditEntry) -> bool:
    """Written by a job, a webhook, a command or the system: not a person. Customers,
    affiliates and planners are people, so their entries stay under "Hide automatic"."""
    source = entry.source or ""
    return source.startswith(AUTOMATIC_PREFIXES) or source == "system"


# --- values ---


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _abbr(text: str, local: datetime, show_abbr: bool) -> str:
    return f"{text} {local.tzname()}" if show_abbr else text


def _trim(amount: Decimal) -> str:
    """`2.50` -> `2.5`, `18.00` -> `18`."""
    return f"{amount.normalize():f}"


def _time(raw: str, zone: str, at: datetime | None, show_abbr: bool) -> str:
    """A wall-clock time in `zone`; the abbreviation is the zone's on the entry's day."""
    tz = _zone(zone)
    day = (at or timezone.now()).astimezone(tz).date()
    local = datetime.combine(day, time.fromisoformat(raw), tzinfo=tz)
    return _abbr(dateformat.format(local, "g:i A"), local, show_abbr)


def _datetime(raw: str, zone: str, show_abbr: bool) -> str:
    """Stored naive UTC (or with an offset); shown in `zone`."""
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(_zone(zone))
    return _abbr(dateformat.format(local, "M j, g:i A"), local, show_abbr)


def _zone_abbr(raw: str, at: datetime | None) -> str:
    try:
        tz = ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        return raw
    return (at or timezone.now()).astimezone(tz).tzname() or raw


def _fk(model: type[models.Model], name: str) -> models.Field | None:
    try:
        f = model._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    return f if f.concrete and (f.many_to_one or f.one_to_one) else None


def format_value(
    model: type[models.Model],
    field_name: str,
    raw: str | None,
    *,
    zone: str,
    fk_names: dict,
    show_abbr: bool = True,
    at: datetime | None = None,
    refund: bool = False,
) -> str:
    """One stored value as it reads on screen (spec §6.3). `at` is the entry's time (for
    a zone's abbreviation on that day); `refund` shows a refund charge's amount negative.
    A value that won't parse is shown as stored rather than breaking the panel."""
    if raw in BLANK:
        return DASH
    raw = str(raw)
    if field_name == "pickup_timezone":
        return _zone_abbr(raw, at)
    try:
        f = model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return raw
    if not f.concrete:  # a reverse relation's name used as a key (`named_passengers`)
        return raw
    try:
        return _by_kind(
            f, raw, zone=zone, fk_names=fk_names, show_abbr=show_abbr, at=at, refund=refund
        )
    except (ValueError, ArithmeticError, ValidationError, TypeError):
        return raw


def _by_kind(f: models.Field, raw: str, *, zone, fk_names, show_abbr, at, refund) -> str:
    name = f.name
    if f.concrete and (f.many_to_one or f.one_to_one):
        related = f.related_model._meta
        return fk_names.get((related.label_lower, raw)) or f"deleted {related.verbose_name} #{raw}"
    if f.choices:
        return str(dict(f.flatchoices).get(f.to_python(raw), raw))
    if isinstance(f, models.BooleanField):
        return {"True": "Yes", "False": "No"}.get(raw, raw)
    if isinstance(f, models.DecimalField | models.IntegerField) and name.endswith("_pct"):
        return f"{_trim(Decimal(raw))}%"
    if isinstance(f, models.DecimalField):
        amount = Decimal(raw)
        if name == "hours" or name.endswith("_hours"):
            return f"{_trim(amount)} {'hr' if amount == 1 else 'hrs'}"
        return money(-amount if refund else amount)
    if isinstance(f, models.DateTimeField):
        return _datetime(raw, zone, show_abbr)
    if isinstance(f, models.DateField):
        return dateformat.format(date.fromisoformat(raw[:10]), "D M j")
    if isinstance(f, models.TimeField):
        return _time(raw, zone, at, show_abbr)
    return raw


# --- foreign keys ---


def _model(entry: AuditEntry) -> type[models.Model] | None:
    ct_field = entry._meta.get_field("content_type")
    if ct_field.is_cached(entry):
        return entry.content_type.model_class()
    return ContentType.objects.get_for_id(entry.content_type_id).model_class()


def fk_targets(entries: Iterable[AuditEntry]) -> dict[str, set[str]]:
    """Every foreign key pk the entries mention, by related model label."""
    targets: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        model = _model(entry)
        if model is None:
            continue
        for key, value in (entry.changes or {}).items():
            fk = _fk(model, key)
            if fk is None or not isinstance(value, list):
                continue
            label = fk.related_model._meta.label_lower
            targets[label].update(str(raw) for raw in value if raw not in BLANK)
    return dict(targets)


def resolve_fks(targets: dict[str, set[str]]) -> dict[tuple[str, str], str]:
    """`(label, pk) -> name`, one `in_bulk` per related model. A missing row is left out,
    so the value reads "deleted <model> #pk"."""
    names: dict[tuple[str, str], str] = {}
    for label, pks in targets.items():
        try:
            model = apps.get_model(label)
        except LookupError:
            continue
        valid = []
        for pk in pks:
            try:
                valid.append(model._meta.pk.to_python(pk))
            except ValidationError:
                continue
        if not valid:
            continue
        for pk, obj in model._base_manager.in_bulk(valid).items():
            names[(label, str(pk))] = registry.label(obj)
    return names


# --- lines ---


def _say(text: str) -> Line:
    return Line("", "", "", sentence=text)


class _NewValues(dict):
    def __missing__(self, key: str) -> str:
        return DASH


def _new_values(changes: dict) -> _NewValues:
    return _NewValues(
        {k: v[1] for k, v in changes.items() if isinstance(v, list) and v[1] not in BLANK}
    )


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def _is_refund(model: type[models.Model], entry: AuditEntry, changes: dict) -> bool:
    if model._meta.label_lower != "payments.charge":
        return False
    refund = model.Kind.REFUND
    kind = changes.get("kind")
    if isinstance(kind, list):
        return refund.value in kind
    return (entry.object_repr or "").startswith(f"{refund.label} ")


def _sort_key(model: type[models.Model], key: str) -> tuple:
    """Field order, then the route stop by stop, then addresses, then anything else: the
    stored JSON's key order is the database's, not the save's."""
    names = [f.name for f in model._meta.fields]
    if key in names:
        return (0, names.index(key), 0, "")
    if route := ROUTE_KEY.fullmatch(key):
        part = route[2]
        return (1, int(route[1]), ROUTE_FIELDS.index(part) + 1 if part in ROUTE_FIELDS else 0, "")
    if address := ADDRESS_KEY.fullmatch(key):
        slot, part = address.groups()
        fields = list(ADDRESS_FIELDS)
        return (
            2,
            list(ADDRESS_SLOTS).index(slot),
            fields.index(part) if part in fields else 99,
            "",
        )
    return (3, 0, 0, key)


def entry_lines(
    entry: AuditEntry, *, zone: str, fk_names: dict, trip_prefix: str = ""
) -> list[Line]:
    """The lines one entry reads as, in `zone` (spec §6.2)."""
    model = _model(entry)
    if model is None:
        lines = [_say(f"Changed {entry.object_repr}")]
    elif entry.action == CREATE:
        lines = [_created(entry, model, fk_names)]
    elif entry.action == DELETE:
        lines = [_removed(entry, model, fk_names)]
    elif entry.action == UPDATE:
        lines = _updated(entry, model, zone, fk_names)
    else:
        lines = []
    for line in lines:
        line.prefix = trip_prefix
    return lines


def _invoice(entry: AuditEntry, model: type[models.Model], side: int, fk_names: dict) -> str:
    raw = (entry.changes or {}).get("invoice", [None, None])[side]
    return format_value(model, "invoice", raw, zone="", fk_names=fk_names)


def _created(entry: AuditEntry, model: type[models.Model], fk_names: dict) -> Line:
    label = model._meta.label_lower
    changes = entry.changes or {}
    if label == "billing.invoiceline":
        amount = money((changes.get("amount") or [None, 0])[1] or 0)
        return _say(f"Invoiced on {_invoice(entry, model, 1, fk_names)} {amount}")
    spec = registry.spec_for(model)
    if spec and "create" in spec.sentences:
        return _say(spec.sentences["create"].format_map(_new_values(changes)))
    return _say(CREATED.get(label) or f"Added {NOUNS.get(label, '')}{entry.object_repr}")


def _removed(entry: AuditEntry, model: type[models.Model], fk_names: dict) -> Line:
    label = model._meta.label_lower
    if label == "billing.invoiceline":
        return _say(f"Removed from {_invoice(entry, model, 0, fk_names)}")
    return _say(f"Removed {NOUNS.get(label, '')}{entry.object_repr}")


def _route_set(changes: dict) -> list[str] | None:
    """The new stops' addresses when the entry sets a route from nothing, else None."""
    whole = {
        int(m[1]): v
        for k, v in changes.items()
        if (m := ROUTE_KEY.fullmatch(k)) and m[2] is None and isinstance(v, list)
    }
    if 1 not in whole or any(old not in BLANK for old, _ in whole.values()):
        return None
    return [str(whole[n][1]) for n in sorted(whole)]


def _updated(entry: AuditEntry, model: type[models.Model], zone: str, fk_names: dict) -> list[Line]:
    changes = entry.changes or {}
    spec = registry.spec_for(model)
    new_values = _new_values(changes)
    fired: dict[str, str] = {}
    for stamp, template in (spec.sentences if spec else {}).items():
        pair = changes.get(stamp)
        if isinstance(pair, list) and pair[0] in BLANK and pair[1] not in BLANK:
            fired[stamp] = template.format_map(new_values)
    absorbed = {name for stamp in fired for name in _placeholders(spec.sentences[stamp])}

    # A trip whose zone changed in this entry: old values in the old zone, new in the new.
    zones = (zone, zone)
    if isinstance(tz := changes.get("pickup_timezone"), list):
        zones = (str(tz[0]) if tz[0] not in BLANK else "", str(tz[1]) if tz[1] not in BLANK else "")

    route = _route_set(changes)
    named = "" if model._meta.label_lower in ANCHORS else f"{entry.object_repr} · "
    refund = _is_refund(model, entry, changes)
    lines: list[Line] = []
    if route:
        lines.append(_say(f"Route set: {' → '.join(route)}"))
    for key in sorted(changes, key=lambda k: _sort_key(model, k)):
        value = changes[key]
        if key in fired:
            lines.append(_say(fired[key]))
        elif key in absorbed or (route and ROUTE_KEY.fullmatch(key)):
            continue
        elif isinstance(value, dict):
            lines.append(_m2m(model, key, value))
        elif isinstance(value, list) and len(value) == 2:
            line = _pair(model, key, value, zones, entry.timestamp, fk_names, refund)
            if not line.sentence:
                line.label = f"{named}{line.label}"
            lines.append(line)
    return lines


def _m2m(model: type[models.Model], key: str, value: dict) -> Line:
    verb = "added" if value.get("operation") == "add" else "removed"
    objects = ", ".join(str(o) for o in value.get("objects") or [])
    return _say(f"{registry.field_label(model, key)}: {verb} {objects}")


def _pair(
    model: type[models.Model],
    key: str,
    value: list,
    zones: tuple[str, str],
    at: datetime,
    fk_names: dict,
    refund: bool,
) -> Line:
    old, new = value
    if route := ROUTE_KEY.fullmatch(key):
        n, part = route.groups()
        if part is None:
            return _say(f"Added stop {n}: {new}" if old in BLANK else f"Removed stop {n}: {old}")
        stop = apps.get_model("reservations", "Stop")
        label = f"Stop {n} {ROUTE_LABELS.get(part) or _verbose(stop, part)}"
        if part in ROUTE_TEXT:
            texts = [DASH if v in BLANK else str(v) for v in value]
        else:
            texts = [
                format_value(stop, part, v, zone=z, fk_names=fk_names, at=at)
                for v, z in zip(value, zones, strict=True)
            ]
    elif address := ADDRESS_KEY.fullmatch(key):
        slot, part = address.groups()
        label = f"{ADDRESS_SLOTS[slot]} · {ADDRESS_FIELDS.get(part) or capfirst(part)}"
        texts = [DASH if v in BLANK else str(v) for v in value]
    else:
        label = registry.field_label(model, key)
        texts = [
            format_value(model, key, v, zone=z, fk_names=fk_names, at=at, refund=refund)
            for v, z in zip(value, zones, strict=True)
        ]
    long = any(len(text) > LONG_TEXT for text in texts)
    return Line(label, texts[0], texts[1], long=long)


def _verbose(model: type[models.Model], name: str) -> str:
    try:
        return str(model._meta.get_field(name).verbose_name)
    except FieldDoesNotExist:
        return name


# --- groups ---


def group_entries(
    entries: Iterable[AuditEntry],
    *,
    zone_for: Callable[[AuditEntry], str],
    fk_names: dict,
    prefix_for: Callable[[AuditEntry], str] = lambda entry: "",
) -> list[Group]:
    """Entries come newest first. Consecutive ones sharing a `cid` (one request, job or
    import) merge into one group; inside it the lines read in the order they happened.
    `day` is the date in the group's zone, for the day headings."""
    runs: list[list[AuditEntry]] = []
    for entry in entries:
        if runs and entry.cid and runs[-1][0].cid == entry.cid:
            runs[-1].append(entry)
        else:
            runs.append([entry])
    groups = []
    for run in runs:
        head = run[0]
        zone = zone_for(head)
        when = head.timestamp.astimezone(_zone(zone))
        lines = [
            line
            for entry in reversed(run)
            for line in entry_lines(
                entry, zone=zone_for(entry), fk_names=fk_names, trip_prefix=prefix_for(entry)
            )
        ]
        groups.append(
            Group(
                who=who(head),
                when=when,
                zone=zone,
                automatic=is_automatic(head),
                lines=lines,
                day=when.date(),
            )
        )
    return groups
