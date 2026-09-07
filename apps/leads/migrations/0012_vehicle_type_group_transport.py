"""`VehicleType.group_transport` — is this class offered as group transport?

Defaults on, so every existing vehicle keeps behaving as it does today. The one-time
backfill turns it off for limousines only: they seat enough to win a capacity-only
match, and the wedding recommender was picking one for a family leg.

A name match is right *here* and wrong at runtime — this is a single pass over the
catalog as it stands today, after which the switch is the client's to set in Settings.
No capacity, rate or minimum is touched.
"""

from django.db import migrations, models

# Substring, case-insensitive: covers "Stretch Limousine", "Limo Bus", "Party Limo".
LIMO = "limo"


def turn_off_for_limos(apps, schema_editor):
    apps.get_model("leads", "VehicleType").objects.filter(name__icontains=LIMO).update(
        group_transport=False
    )


def turn_back_on(apps, schema_editor):
    """Reverse leaves the column as it was created — every row eligible."""
    apps.get_model("leads", "VehicleType").objects.filter(name__icontains=LIMO).update(
        group_transport=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0011_alter_lead_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="vehicletype",
            name="group_transport",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(turn_off_for_limos, turn_back_on),
    ]
