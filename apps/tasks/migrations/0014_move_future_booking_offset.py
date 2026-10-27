"""Move the future-booking offset from `TaskConfig` to a `TaskSchedule` row."""

from django.db import migrations

DEFAULT_DAYS = 330


def schedule_row_for(offset: int) -> dict | None:
    """The row that carries a non-default offset, or None when the code default already does."""
    if offset == DEFAULT_DAYS:
        return None
    return {
        "kind": "future_booking_followup",
        "variant": "standard",
        "amount": offset,
        "unit": "days",
    }


def copy_offset(apps, schema_editor):
    TaskConfig = apps.get_model("tasks", "TaskConfig")
    TaskSchedule = apps.get_model("tasks", "TaskSchedule")
    config = TaskConfig.objects.first()
    row = schedule_row_for(config.future_booking_offset_days) if config else None
    if row:
        TaskSchedule.objects.update_or_create(
            kind=row["kind"],
            variant=row["variant"],
            defaults={"amount": row["amount"], "unit": row["unit"]},
        )


class Migration(migrations.Migration):
    dependencies = [("tasks", "0013_taskschedule")]

    operations = [
        migrations.RunPython(copy_offset, migrations.RunPython.noop),
        migrations.RemoveField(model_name="taskconfig", name="future_booking_offset_days"),
    ]
