from django.db import migrations, models

from apps.addresses.loaders import load_airports


def fill_timezones(apps, schema_editor):
    load_airports(apps.get_model("addresses", "Airport"))


class Migration(migrations.Migration):
    dependencies = [
        ("addresses", "0004_airline"),
    ]

    operations = [
        migrations.AddField(
            model_name="airport",
            name="timezone",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.RunPython(fill_timezones, migrations.RunPython.noop),
    ]
