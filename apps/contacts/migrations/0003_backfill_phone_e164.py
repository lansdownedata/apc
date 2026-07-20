from django.db import migrations

from apps.contacts.services import backfill_phone_e164


def forwards(apps, schema_editor):
    backfill_phone_e164(apps.get_model("contacts", "Contact"))


def backwards(apps, schema_editor):
    """No-op. E.164 is a valid phone string, and the original formatting is not recoverable."""


class Migration(migrations.Migration):
    dependencies = [("contacts", "0002_contact_podium_contact_uid")]

    operations = [migrations.RunPython(forwards, backwards)]
