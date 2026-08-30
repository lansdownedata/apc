from django.db import migrations, models

from apps.addresses.loaders import load_airports


def fill_flags_and_load_global_airports(apps, schema_editor):
    """Same pattern as 0005's timezone backfill: reload the (now-extended) committed CSV.

    This single reload does two things at once: it sets `serves_ground_transport` /
    `has_scheduled_service` on the 863 existing rows from the CSV's new columns, and it
    inserts the ~2,774 global scheduled-service airports + US territories that the CSV
    now also carries (2026-08-29 flight-verification data expansion — see
    apps/addresses/data/airports.csv and the airport-data report). The existing 863 keep
    `serves_ground_transport=True`; the global additions are `False` except the 11 US
    territory airports (PR/VI/GU), which are `True`.
    """
    load_airports(apps.get_model("addresses", "Airport"))


class Migration(migrations.Migration):
    dependencies = [
        ("addresses", "0005_airport_timezone"),
    ]

    operations = [
        migrations.AddField(
            model_name="airport",
            name="has_scheduled_service",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="airport",
            name="serves_ground_transport",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(fill_flags_and_load_global_airports, migrations.RunPython.noop),
    ]
