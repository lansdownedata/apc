from django.db import migrations

# The messages this migration seeds, frozen: later seed migrations own the keys they add, so a
# fresh database never seeds those here against this migration's historical models.
KEYS = (
    "tp1_welcome",
    "tp2_lead_followup",
    "tp3_quote_sent_sms",
    "tp4_viewed_sms",
    "tp5_viewed_email",
    "tp6_quote_followup",
    "tp7_expiring",
    "tp8_expired",
    "review_request",
    "thank_you",
    "thank_you_wedding",
    "payment_reminder",
    "wed_final_details",
    "trip_confirm_customer",
    "trip_confirm_customer_2",
    "trip_confirm_affiliate",
    "driver_released",
    "driver_updated",
    "status_dispatched",
    "status_on_the_way",
    "status_arrived",
    "order_auth_expired",
    "order_confirmed",
    "order_cancelled",
)


def seed_rows(apps, schema_editor):
    from apps.messaging import defaults

    defaults.seed(apps, keys=KEYS)


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0020_remove_lead_passenger_names"),
        ("messaging", "0021_message_rows"),
    ]

    operations = [migrations.RunPython(seed_rows, migrations.RunPython.noop)]
