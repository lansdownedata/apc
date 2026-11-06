"""APC-131: a combined send records which trips it listed for each person."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("messaging", "0030_confirmation_names_every_trip")]

    operations = [
        migrations.AddField(
            model_name="delivery",
            name="covers",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
