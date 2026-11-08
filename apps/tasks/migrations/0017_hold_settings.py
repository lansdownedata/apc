"""APC-95: the cleaning fee moves from `TaskConfig` onto each vehicle type, and the hold
settings arrive (off).

The old single fee lands on every LARGE (bus / van / stretch) type when it was set; the
client then sets each type's own number. The copy runs before the column is dropped, in
this same migration, so it still reads the old value.
"""

import datetime
from decimal import Decimal

from django.db import migrations, models


def _old_fee(apps) -> Decimal:
    TaskConfig = apps.get_model("tasks", "TaskConfig")
    fee = TaskConfig.objects.filter(pk=1).values_list("cleaning_fee", flat=True).first()
    return Decimal(fee or 0)


def copy_fee(apps, schema_editor):
    fee = _old_fee(apps)
    if fee <= 0:
        return
    VehicleType = apps.get_model("leads", "VehicleType")
    VehicleType.objects.filter(overtime_class="large").update(cleaning_fee=fee)


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0016_remove_report_emails"),
        ("leads", "0022_vehicletype_cleaning_fee"),
    ]

    operations = [
        migrations.RunPython(copy_fee, migrations.RunPython.noop),
        migrations.RemoveField(model_name="taskconfig", name="cleaning_fee"),
        migrations.AddField(
            model_name="taskconfig",
            name="hold_enabled",
            field=models.BooleanField(default=False, verbose_name="Place cleaning holds"),
        ),
        migrations.AddField(
            model_name="taskconfig",
            name="hold_place_time",
            field=models.TimeField(
                default=datetime.time(3, 0),
                help_text="On the day of the order's first held trip, in the trip's local time.",
                verbose_name="Place the hold at",
            ),
        ),
        migrations.AddField(
            model_name="taskconfig",
            name="hold_release_days",
            field=models.PositiveSmallIntegerField(
                default=3,
                help_text="Days after the order's last held trip.",
                verbose_name="Release an unused hold no later than",
            ),
        ),
    ]
