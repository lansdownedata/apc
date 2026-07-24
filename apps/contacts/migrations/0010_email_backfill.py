from collections import defaultdict

from django.db import migrations


def forwards(apps, schema_editor):
    Contact = apps.get_model("contacts", "Contact")
    # 1) normalize "" -> None, lowercase/trim
    for pk, email in Contact.objects.values_list("pk", "email"):
        norm = (email or "").strip().lower() or None
        if norm != email:
            Contact.objects.filter(pk=pk).update(email=norm)
    # 2) dedupe case-insensitive collisions: newest row keeps the email, older -> NULL
    groups = defaultdict(list)
    for pk, email in Contact.objects.exclude(email=None).values_list("pk", "email"):
        groups[email].append(pk)
    for email, pks in groups.items():
        if len(pks) > 1:
            keep = max(pks)  # newest by pk
            losers = [p for p in pks if p != keep]
            Contact.objects.filter(pk__in=losers).update(email=None)
            print(f"  email backfill: '{email}' shared by {sorted(pks)} — kept {keep}, cleared {losers}")


class Migration(migrations.Migration):
    dependencies = [("contacts", "0009_email_nullable")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
