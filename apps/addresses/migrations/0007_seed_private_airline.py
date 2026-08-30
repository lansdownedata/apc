# Generated for the 2026-08-29 private/tail-number flight feature.

from django.db import migrations

from apps.addresses.loaders import load_airlines


def seed(apps, schema_editor):
    """Reload the committed CSV — idempotent (keyed on `iata`), so this only adds the new
    "N" / Private row without touching any carrier migration 0004 already seeded."""
    load_airlines(apps.get_model("addresses", "Airline"))


def unseed(apps, schema_editor):
    apps.get_model("addresses", "Airline").objects.filter(iata="N").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("addresses", "0006_airport_transport_flags"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
