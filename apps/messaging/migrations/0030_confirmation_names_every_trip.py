"""APC-123: the trip confirmation's text says "trip" or "trips" from how many trips the day's
notice covers, and names every pickup time.

The seeded wording moves from `{trip_pickup_when}` inside a singular sentence to the
`{trips_coming_up}` sentence, which reads the same as before for one trip. A row an Admin
has edited keeps its own wording. Both texts are frozen here.
"""

from django.db import migrations

OLD = (
    "{first_name}, it's {company_name}. Your trip on {trip_pickup_when} is coming "
    "up. Please confirm the details and let us know of any changes: {confirm_link}"
)
NEW = (
    "{first_name}, it's {company_name}. {trips_coming_up}. Please confirm the "
    "details and let us know of any changes: {confirm_link}"
)


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    MessageTemplate.objects.filter(key="trip_confirm_customer", sms_body=OLD).update(sms_body=NEW)


def backwards(apps, schema_editor):
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    MessageTemplate.objects.filter(key="trip_confirm_customer", sms_body=NEW).update(sms_body=OLD)


class Migration(migrations.Migration):
    dependencies = [("messaging", "0029_notification_quiet_hours")]

    operations = [migrations.RunPython(forwards, backwards)]
