"""The customer invite said "Your past trips with us are already in your account." to every
contact, including one who has never ridden with us. It now says "Any past trips …", which
is true either way. A database that already seeded the row (0034) keeps its wording unless
this changes it; a row an Admin has reworded is left alone. Both sentences are frozen here.
"""

from django.db import migrations

KEY = "customer_invite"
OLD = "Your past trips with us are already in your account."
NEW = "Any past trips with us are already in your account."
BODY = (
    "Hi {first_name},\n\n"
    "You can now request trips, follow your bookings and download receipts online.\n\n"
    "%s\n\n"
    "{account_button}\n\n"
    "This link works for 7 days. Questions? Call {company_phone}."
)


def _reword(apps, was: str, now: str) -> None:
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    MessageTemplate.objects.filter(key=KEY, email_body=BODY % was).update(email_body=BODY % now)


def forwards(apps, schema_editor):
    _reword(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _reword(apps, NEW, OLD)


class Migration(migrations.Migration):
    dependencies = [("messaging", "0035_alter_message_channel")]

    operations = [migrations.RunPython(forwards, backwards)]
