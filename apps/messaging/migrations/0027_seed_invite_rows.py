from django.db import migrations

# The invite, password-reset and wedding-details messages this migration seeds, frozen:
# later seed migrations own the keys they add (see 0022).
KEYS = (
    "planner_invite",
    "planner_password_reset",
    "user_invite",
    "wedding_details_received",
)


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    defaults.seed(apps, keys=KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0026_seed_staff_click_rows"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
