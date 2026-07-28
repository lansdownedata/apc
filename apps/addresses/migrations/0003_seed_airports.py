from django.db import migrations

from apps.addresses.loaders import load_airports


def seed(apps, schema_editor):
    load_airports(apps.get_model("addresses", "Airport"))


def unseed(apps, schema_editor):
    apps.get_model("addresses", "Airport").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("addresses", "0002_airport")]
    operations = [migrations.RunPython(seed, unseed)]
