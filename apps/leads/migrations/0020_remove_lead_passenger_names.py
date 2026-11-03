from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0019_lead_billing_group"),
        ("reservations", "0029_passengers_from_names"),
    ]

    operations = [
        migrations.RemoveField(model_name="lead", name="passenger_names"),
    ]
