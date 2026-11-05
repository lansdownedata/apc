from django.db import migrations

# The staff-click messages this migration seeds, frozen: later seed migrations own the keys
# they add (see 0022).
KEYS = (
    "send_quote",
    "send_quote_on_account",
    "send_pay_link",
    "send_terms_link",
    "payment_receipt",
    "affiliate_offer",
    "affiliate_cancel",
)


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    defaults.seed(apps, keys=KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0025_touchpoint_lead_optional"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
