# A transfer is priced as rate × transfer_min_hours (spec 2026-08-28). A vehicle with a
# 0 minimum would price every transfer at $0, so 0 becomes 1 = "flat rate".
from decimal import Decimal

from django.db import migrations, models


def backfill_transfer_minimum(VehicleType) -> int:
    return VehicleType.objects.filter(transfer_min_hours=0).update(
        transfer_min_hours=Decimal("1")
    )


def forwards(apps, schema_editor):
    backfill_transfer_minimum(apps.get_model("leads", "VehicleType"))


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0006_alter_vehicletype_hourly_min_hours_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vehicletype",
            name="transfer_min_hours",
            field=models.DecimalField(blank=True, decimal_places=2, default=1, max_digits=5),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
