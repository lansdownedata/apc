"""Contact maintenance routines."""

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
