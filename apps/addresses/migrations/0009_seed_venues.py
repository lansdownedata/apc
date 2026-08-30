# Seeds the wedding venue / hotel / ceremony-site directory (spec 2026-08-30 §6.1).
# A data migration rather than a manual command run so a deploy carries the directory —
# Heroku runs `migrate` on release and nothing else.

from django.db import migrations

from apps.addresses.loaders import load_venues


def seed(apps, schema_editor):
    load_venues(apps.get_model("addresses", "Venue"))


def unseed(apps, schema_editor):
    apps.get_model("addresses", "Venue").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("addresses", "0008_venue")]
    operations = [migrations.RunPython(seed, unseed)]
