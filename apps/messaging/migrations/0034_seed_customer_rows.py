from django.db import migrations

# The customer portal's account emails this migration seeds, frozen: later seed migrations
# own the keys they add (see 0022).
KEYS = ("customer_invite", "customer_signup", "customer_password_reset")


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    defaults.seed(apps, keys=KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0033_driver_link_rows"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
