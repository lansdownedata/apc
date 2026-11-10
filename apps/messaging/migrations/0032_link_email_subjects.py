"""Every email goes through our own mail now (2026-11-10), so the payment and terms links'
emails, which went by Podium with no subject line, get one. A subject an Admin already wrote
stays. Both subjects are frozen here.
"""

from django.db import migrations

SUBJECTS = {
    "send_pay_link": "Your {company_name} payment link",
    "send_terms_link": "Terms for your {company_name} reservation {quote_no}",
}


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    for key, subject in SUBJECTS.items():
        MessageTemplate.objects.filter(key=key, subject="").update(subject=subject)


def backwards(apps, schema_editor):
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    for key, subject in SUBJECTS.items():
        MessageTemplate.objects.filter(key=key, subject=subject).update(subject="")


class Migration(migrations.Migration):
    dependencies = [("messaging", "0031_delivery_covers")]

    operations = [migrations.RunPython(forwards, backwards)]
