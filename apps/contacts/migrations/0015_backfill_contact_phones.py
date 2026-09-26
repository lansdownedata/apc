from django.db import migrations

from apps.contacts.services import backfill_contact_phones


def forwards(apps, schema_editor):
    backfill_contact_phones(
        apps.get_model("contacts", "Contact"), apps.get_model("contacts", "ContactPhone")
    )


def backwards(apps, schema_editor):
    """No-op. Dropping the table (0014 reversed) removes the rows."""


class Migration(migrations.Migration):
    dependencies = [("contacts", "0014_contact_phones")]

    operations = [migrations.RunPython(forwards, backwards)]
