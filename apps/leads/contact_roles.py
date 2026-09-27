"""Contact roles on an order (APC-64) — every LeadContact write, and the day-of reader.

The day-of contact used to be two free-text columns on `Lead`. It's now a contact in the
`DAY_OF_COORDINATOR` role. For one release both are written (`set_day_of_contact`), and
every reader goes through `day_of_contact` — the role, not the columns.

People are found the way contact creation finds them (`contacts.models.match_contact`:
email decides, else any number on file). A match is never renamed if it's anyone's
customer; a contact that only exists as someone's role can be.

`resolve_contact` and `backfill_day_of_roles` take model classes so the data migration
runs them on its historical models (the `backfill_contact_phones` precedent) — which have
none of `Contact.save`'s phone mirroring, hence the explicit texting row.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.contacts.models import Contact, ContactPhone, match_contact
from apps.core.choices import Channel
from apps.core.phone import to_e164

from .models import Lead, LeadContact

DAY_OF = LeadContact.Role.DAY_OF_COORDINATOR


class RoleError(Exception):
    """A People-card action the order can't take; the message is shown to staff."""


def resolve_contact(contact_model, phone_model, *, name: str, phone: str, reuse=None):
    """The contact behind a typed name + phone: a phone match, else `reuse` (the role's
    current contact, when it has no number of its own), else a new contact.

    A match that is nobody's customer takes the typed name — that's a staff correction.
    """
    name, phone = (name or "").strip()[:200], (phone or "").strip()
    number = to_e164(phone) or phone[:32]
    found = match_contact(contact_model, phone_model, phone=phone) if phone else None
    if found is None and reuse is not None and not reuse.phone and not phone:
        found = reuse
    if found is not None:
        if name and name != found.name and not found.leads.exists():
            found.name = name
            found.save(update_fields=["name", "updated_at"])
        return found
    contact = contact_model.objects.create(
        name=name or number, phone=number, channel=Channel.PHONE, email=None
    )
    if number:
        phone_model.objects.get_or_create(
            contact_id=contact.pk, number=number, defaults={"label": "mobile", "texting": True}
        )
    return contact


def backfill_day_of_roles(lead_model, contact_model, phone_model, lead_contact_model) -> int:
    """Give every lead with a day-of contact in the old columns its DAY_OF_COORDINATOR
    row. Leads that already have one are skipped, so it's safe to run again. Returns the
    number of roles created."""
    have = set(lead_contact_model.objects.filter(role=DAY_OF).values_list("lead_id", flat=True))
    created = 0
    legacy = lead_model.objects.exclude(day_of_contact_name="", day_of_contact_phone="")
    for lead in legacy.exclude(pk__in=have).order_by("pk"):
        contact = resolve_contact(
            contact_model,
            phone_model,
            name=lead.day_of_contact_name,
            phone=lead.day_of_contact_phone,
        )
        lead_contact_model.objects.create(lead_id=lead.pk, contact_id=contact.pk, role=DAY_OF)
        created += 1
    return created


# --- reading ------------------------------------------------------------------------


def day_of_contact(lead: Lead) -> tuple[str, str]:
    """(name, phone) of the order's day-of coordinator, or ("", "")."""
    row = (
        LeadContact.objects.filter(lead=lead, role=DAY_OF)
        .select_related("contact")
        .order_by("created_at", "pk")
        .first()
    )
    return (row.contact.name, row.contact.phone) if row else ("", "")


def people(lead: Lead) -> list[LeadContact]:
    """The People card's rows, in role order. One query."""
    order = {value: i for i, value in enumerate(LeadContact.Role.values)}
    rows = LeadContact.objects.filter(lead=lead).select_related("contact")
    return sorted(rows, key=lambda r: (order.get(r.role, 99), r.created_at, r.pk))


# --- writing ------------------------------------------------------------------------


def _write_columns(lead: Lead, name: str, phone: str) -> None:
    """The transition release keeps `Lead.day_of_contact_*` in step with the role."""
    Lead.objects.filter(pk=lead.pk).update(day_of_contact_name=name, day_of_contact_phone=phone)
    lead.day_of_contact_name, lead.day_of_contact_phone = name, phone


def set_day_of_contact(lead: Lead, *, name: str, phone: str) -> LeadContact | None:
    """Point the order's day-of coordinator at the person typed in (both blank = none)."""
    name = (name or "").strip()[:200]
    raw = (phone or "").strip()
    number = to_e164(raw) or raw[:32]
    with transaction.atomic():
        current = LeadContact.objects.filter(lead=lead, role=DAY_OF).select_related("contact")
        if not name and not number:
            current.delete()
            _write_columns(lead, "", "")
            return None
        existing = current.first()
        contact = resolve_contact(
            Contact,
            ContactPhone,
            name=name,
            phone=number,
            reuse=existing.contact if existing else None,
        )
        current.exclude(contact=contact).delete()
        row, _ = LeadContact.objects.get_or_create(lead=lead, contact=contact, role=DAY_OF)
        _write_columns(lead, name or contact.name, number)
    return row


def add_person(lead: Lead, *, role: str, contact: str, phone: str = "") -> LeadContact:
    """The People card's add: `contact` is an existing contact's pk, or a name typed into
    the picker (created through the same dedupe as everywhere else)."""
    if role not in LeadContact.Role.values:
        raise RoleError("Pick a role.")
    value = (contact or "").strip()
    if not value:
        raise RoleError("Pick a person, or type a name to add someone new.")
    if value.isdigit():
        person = Contact.objects.filter(pk=int(value)).first()
        if person is None:
            raise RoleError("That contact no longer exists.")
    else:
        person = resolve_contact(Contact, ContactPhone, name=value, phone=phone)
    if role == DAY_OF:
        # The old columns carry one day-of contact; the newest one wins until they go.
        _write_columns(lead, person.name, person.phone)
    try:
        with transaction.atomic():
            return LeadContact.objects.create(lead=lead, contact=person, role=role)
    except IntegrityError:
        raise RoleError(
            f"{person.name} is already the {LeadContact.Role(role).label.lower()} on this order."
        ) from None


def remove_person(row: LeadContact) -> None:
    lead, was_day_of = row.lead, row.role == DAY_OF
    row.delete()
    if was_day_of:
        name, phone = day_of_contact(lead)
        _write_columns(lead, name, phone)


# --- the People card ------------------------------------------------------------------

# The picker lists contacts client-side (Tom Select); past this many, the newest win.
PICKER_LIMIT = 1000


def people_context(lead: Lead) -> dict:
    """What `leads/_people_card.html` and the workspace header read. Three queries."""
    day_of_name, day_of_phone = day_of_contact(lead)
    contacts = Contact.objects.order_by("-created_at").values_list("pk", "name", "phone")
    options = sorted(
        (
            (pk, f"{name} · {phone}" if phone else name)
            for pk, name, phone in contacts[:PICKER_LIMIT]
        ),
        key=lambda o: o[1].lower(),
    )
    return {
        "people": people(lead),
        "role_choices": LeadContact.Role.choices,
        "people_contact_options": options,
        "day_of_name": day_of_name,
        "day_of_phone": day_of_phone,
    }
