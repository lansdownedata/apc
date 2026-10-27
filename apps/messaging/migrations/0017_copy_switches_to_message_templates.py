from django.db import migrations

# NotificationConfig field stem -> message kind (all identical except the wedding one).
STEMS = {
    "wedding_final_details": "wed_final_details",
    "trip_confirm_customer": "trip_confirm_customer",
    "trip_confirm_affiliate": "trip_confirm_affiliate",
    "driver_released": "driver_released",
    "status_dispatched": "status_dispatched",
    "status_on_the_way": "status_on_the_way",
    "status_arrived": "status_arrived",
    "order_auth_expired": "order_auth_expired",
    "order_confirmed": "order_confirmed",
    "order_cancelled": "order_cancelled",
}
DEFAULT_OFF = {"status_dispatched", "status_on_the_way", "status_arrived"}


def rows_for(flags: dict[str, bool]) -> dict[str, bool]:
    """The kinds whose old switch differs from the new default, with the switch to store."""
    return {
        kind: enabled for kind, enabled in flags.items() if enabled != (kind not in DEFAULT_OFF)
    }


def copy_switches(apps, schema_editor):
    NotificationConfig = apps.get_model("messaging", "NotificationConfig")
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    config = NotificationConfig.objects.first()
    if config is None:
        return
    flags = {kind: getattr(config, f"{stem}_enabled") for stem, kind in STEMS.items()}
    for kind, enabled in rows_for(flags).items():
        MessageTemplate.objects.update_or_create(kind=kind, defaults={"enabled": enabled})


class Migration(migrations.Migration):
    dependencies = [("messaging", "0016_messagetemplate")]

    operations = [migrations.RunPython(copy_switches, migrations.RunPython.noop)]
