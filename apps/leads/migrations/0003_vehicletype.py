from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0002_lead_quote_expires_at_lead_quote_sent_at_and_more"),
        # Force this rename to run AFTER the data migration that seeds rows via
        # apps.get_model("leads", "Vehicle") — that migration's historical model state
        # must still be "Vehicle" when it runs. Without this explicit dependency, Django's
        # topological sort has no ordering constraint between the two and could run this
        # rename first, breaking apps.get_model("leads", "Vehicle") on a fresh migrate.
        ("reservations", "0003_seed_vehicles"),
    ]

    operations = [
        migrations.RenameModel(old_name="Vehicle", new_name="VehicleType"),
        migrations.RemoveField(model_name="vehicletype", name="klass"),
        migrations.AddField(
            model_name="vehicletype",
            name="image",
            field=models.ImageField(blank=True, upload_to="vehicle-types/"),
        ),
        migrations.AddField(
            model_name="vehicletype", name="description", field=models.TextField(blank=True)
        ),
        migrations.AddField(
            model_name="vehicletype",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterModelOptions(
            name="vehicletype", options={"ordering": ["sort_order", "name"]}
        ),
    ]
