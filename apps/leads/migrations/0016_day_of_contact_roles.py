from django.db import migrations

from apps.leads.contact_roles import backfill_day_of_roles


def forwards(apps, schema_editor):
    backfill_day_of_roles(
        apps.get_model("leads", "Lead"),
        apps.get_model("contacts", "Contact"),
        apps.get_model("contacts", "ContactPhone"),
        apps.get_model("leads", "LeadContact"),
    )


def backwards(apps, schema_editor):
    """No-op. The old `day_of_contact_*` columns are still written for this release, and
    dropping the table (0015 reversed) removes the rows."""


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0015_leadcontact"),
        ("contacts", "0015_backfill_contact_phones"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
