"""Driver info released now follows the Driver details message: move its row there."""

from django.db import migrations

DEFAULT_HOURS = 12
MINIMUM_HOURS = 1


def message_timing_for(amount: int, unit: str) -> tuple[int, str] | None:
    """The message timing that carries a checklist row's value, or None when it is the
    default. Days become hours; under an hour becomes the message minimum of 1 hour."""
    hours = amount * 24 if unit == "days" else amount
    hours = max(hours, MINIMUM_HOURS)
    if hours == DEFAULT_HOURS:
        return None
    return hours, "hours"


def move_row(apps, schema_editor):
    TaskSchedule = apps.get_model("tasks", "TaskSchedule")
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    rows = TaskSchedule.objects.filter(kind="driver_released")
    row = rows.filter(variant="standard").first()
    timing = message_timing_for(row.amount, row.unit) if row else None
    if timing:
        template, _ = MessageTemplate.objects.get_or_create(kind="driver_released")
        if template.timing_amount is None:
            template.timing_amount, template.timing_unit = timing
            template.save(update_fields=["timing_amount", "timing_unit"])
    rows.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0014_move_future_booking_offset"),
        ("messaging", "0020_driver_updated_kind"),
    ]

    operations = [migrations.RunPython(move_row, migrations.RunPython.noop)]
