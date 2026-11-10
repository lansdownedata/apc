"""The "Final balance paid" task is now due an interval after it opens, not days before pickup
(TASK-03). A saved Settings row meant "due N before pickup" with the task opening 30 days
before pickup at midnight; it becomes the same gap counted from the opening."""

from django.db import migrations

OPENED_DAYS_BEFORE = 30  # the opening the saved rows were written against
DEFAULT = (48, "hours")


def interval_for(amount: int, unit: str) -> tuple[int, str] | None:
    """The after-opening interval a saved before-pickup row carries, or None when it is the
    new default (2 days). A row that was due before the task opened becomes due as it opens."""
    if unit == "days":
        days = max(0, OPENED_DAYS_BEFORE - amount)
        return None if days * 24 == DEFAULT[0] else (days, "days")
    hours = max(0, OPENED_DAYS_BEFORE * 24 - amount)
    return None if hours == DEFAULT[0] else (hours, "hours")


def convert(apps, schema_editor):
    TaskSchedule = apps.get_model("tasks", "TaskSchedule")
    row = TaskSchedule.objects.filter(kind="final_balance_paid", variant="standard").first()
    if row is None:
        return
    interval = interval_for(row.amount, row.unit)
    if interval is None:
        row.delete()
        return
    row.amount, row.unit = interval
    row.save(update_fields=["amount", "unit"])


class Migration(migrations.Migration):
    dependencies = [("tasks", "0018_hold_lead_hours")]

    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
