from django.db import migrations

from apps.core.phone import to_e164


def backfill(apps, schema_editor):
    """One ContactPhone per non-blank legacy_phone, marked primary.

    Rows that will not normalize are left in legacy_phone and reported — never dropped.
    Duplicates across contacts are skipped (e164 is unique); the earliest contact wins.
    """
    Contact = apps.get_model("contacts", "Contact")
    ContactPhone = apps.get_model("contacts", "ContactPhone")

    seen: set[str] = set()
    unparseable: list[int] = []
    collisions: list[int] = []

    for contact in Contact.objects.exclude(legacy_phone="").order_by("created_at").iterator():
        e164 = to_e164(contact.legacy_phone)
        if e164 is None:
            unparseable.append(contact.pk)
            continue
        if e164 in seen:
            collisions.append(contact.pk)
            continue
        seen.add(e164)
        ContactPhone.objects.create(
            contact_id=contact.pk, e164=e164, is_primary=True, label=""
        )

    if unparseable:
        print(f"  ! {len(unparseable)} contact(s) had unparseable phones: {unparseable}")
    if collisions:
        print(f"  ! {len(collisions)} contact(s) shared a number with an earlier contact: {collisions}")


def unbackfill(apps, schema_editor):
    apps.get_model("contacts", "ContactPhone").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("contacts", "0003_contactphone")]
    operations = [migrations.RunPython(backfill, unbackfill)]
