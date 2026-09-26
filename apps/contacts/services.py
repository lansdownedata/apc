"""Contact maintenance routines."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.core.phone import to_e164


class PhoneError(ValueError):
    """A phone change that can't be made; the message is safe to show an agent."""


def _dialable(number: str) -> str:
    normalized = to_e164(number or "")
    if not normalized:
        raise PhoneError("Enter a valid phone number.")
    return normalized


def add_phone(contact, number: str, *, label: str = "", texting: bool = False):
    """Put a number on file for `contact`, or return the row already holding it.

    It becomes the texting number when asked to, or when the contact has none yet —
    otherwise the number Podium texts stays exactly where it was.
    """
    from apps.contacts.models import ContactPhone  # local import: models imports this module

    number = _dialable(number)
    row = contact.phones.filter(number=number).first()
    if row is None:
        row = contact.phones.create(number=number, label=label or ContactPhone.Label.MOBILE)
    elif label and row.label != label:
        row.label = label
        row.save(update_fields=["label", "updated_at"])
    if texting or not contact.phone:
        use_for_texting(row)
    return row


def use_for_texting(row) -> None:
    """Make `row` the number Podium texts. `Contact.save` moves the flag."""
    contact = row.contact
    contact.phone = row.number
    contact.save(update_fields=["phone", "updated_at"])
    row.texting = True


def update_phone(row, *, number: str, label: str) -> None:
    """Change a number's digits and label; the texting number carries Contact.phone along."""
    number = _dialable(number)
    if number != row.number and row.contact.phones.filter(number=number).exists():
        raise PhoneError("That number is already on this contact.")
    row.number, row.label = number, label or row.label
    row.save(update_fields=["number", "label", "updated_at"])
    if row.texting:
        use_for_texting(row)


def remove_phone(row) -> None:
    """Delete a number. The texting one only goes when it is the last number on file,
    so a customer is never silently moved to a different Podium thread."""
    contact = row.contact
    if row.texting and contact.phones.exclude(pk=row.pk).exists():
        raise PhoneError("Choose another number for texting before removing this one.")
    row.delete()
    if row.texting:
        contact.phone = ""
        contact.save(update_fields=["phone", "updated_at"])


def backfill_phone_e164(contact_model) -> int:
    """Rewrite stored phones to canonical E.164. Returns the number of rows changed.

    Rows `to_e164` cannot parse are left exactly as they are — a bad number is still
    the only way to reach that customer, and blanking it loses information we cannot
    recover. Takes the model class so a migration can pass its historical version.
    """
    updated = 0
    for pk, phone in contact_model.objects.exclude(phone="").values_list("pk", "phone"):
        normalized = to_e164(phone)
        if normalized and normalized != phone:
            contact_model.objects.filter(pk=pk).update(phone=normalized)
            updated += 1
    return updated


def backfill_contact_phones(contact_model, phone_model) -> int:
    """Give every contact's existing `phone` its ContactPhone row, marked texting.

    Takes the model classes so a migration can pass its historical versions (which
    have none of `Contact.save`'s syncing). Returns the number of rows created.
    """
    have = set(phone_model.objects.values_list("contact_id", "number"))
    rows = [
        phone_model(contact_id=pk, number=phone, label="mobile", texting=True)
        for pk, phone in contact_model.objects.exclude(phone="").values_list("pk", "phone")
        if (pk, phone) not in have
    ]
    phone_model.objects.bulk_create(rows)
    return len(rows)


def apply_booking_edits(
    contact,
    *,
    name: str = "",
    company: str = "",
    phone: str = "",
    email: str = "",
) -> str | None:
    """Write the contact modal's edits back onto a customer the agent explicitly picked.

    Only non-blank values are applied: a blank field in the modal means "I didn't fill
    this in", never "erase what's on file". A phone is added as another number rather
    than replacing the one Podium texts. Clearing a value is done on the contact
    profile, where it reads as a deliberate act.

    `channel` is pointedly absent — that dropdown is the *lead's* source, while
    `Contact.channel` records how the customer first found us and does not change on
    their fifth booking.

    Returns a warning to surface to the agent, or None when everything applied.
    """
    from apps.contacts.models import Company  # local import: models imports nothing here

    if phone.strip():
        # Added, never swapped: which number Podium texts changes only on the profile.
        try:
            add_phone(contact, phone)
        except PhoneError:
            pass  # the modal validates the number; a legacy value is not worth a failed booking
    updates: dict[str, object] = {}
    if name.strip():
        updates["name"] = name.strip()
    if email.strip():
        updates["email"] = email.strip()
    if company.strip():
        updates["company"] = Company.objects.get_or_create_by_name(company)

    changed = {f: v for f, v in updates.items() if getattr(contact, f) != v}
    if not changed:
        return None

    def _save(fields: dict) -> None:
        for field, value in fields.items():
            setattr(contact, field, value)
        contact.save(update_fields=[*fields, "updated_at"])

    try:
        # Savepoint, not a bare try: a failed statement poisons the surrounding
        # transaction, so without this the caller's next query raises
        # TransactionManagementError instead of the booking going through.
        with transaction.atomic():
            _save(changed)
    except IntegrityError:
        # The only unique constraint here is the case-insensitive email. Losing the
        # booking over a duplicate address would be the wrong trade — keep the stored
        # email, save the rest, and tell the agent what was skipped.
        contact.refresh_from_db()
        rest = {f: v for f, v in changed.items() if f != "email"}
        if rest:
            _save(rest)
        return (
            f"That email address belongs to another customer, so {contact.name}'s "
            "email was left unchanged."
        )
    return None
