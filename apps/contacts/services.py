"""Contact maintenance routines."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.core.phone import to_e164


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
    this in", never "erase what's on file". Clearing a value is done on the contact
    profile, where it reads as a deliberate act.

    `channel` is pointedly absent — that dropdown is the *lead's* source, while
    `Contact.channel` records how the customer first found us and does not change on
    their fifth booking.

    Returns a warning to surface to the agent, or None when everything applied.
    """
    from apps.contacts.models import Company  # local import: models imports nothing here

    updates: dict[str, object] = {}
    if name.strip():
        updates["name"] = name.strip()
    if phone.strip():
        updates["phone"] = phone.strip()
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
