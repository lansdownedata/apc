from django.db import migrations


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    defaults.seed(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0020_remove_lead_passenger_names"),
        ("messaging", "0021_message_rows"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
