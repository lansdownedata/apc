from django.db import migrations

# (name, applies_to, sort_order) — only the types the client named; the rest he adds in
# Settings. Editable afterwards; unseed removes by name so a renamed row survives.
SEED = [
    ("Driver's license", "driver", 0),
    ("Registration", "vehicle", 0),
    ("State inspection", "vehicle", 1),
    ("Airport permit", "vehicle", 2),
]


def seed(apps, schema_editor):
    RenewalType = apps.get_model("fleet", "RenewalType")
    for name, applies_to, order in SEED:
        RenewalType.objects.get_or_create(
            name=name, applies_to=applies_to, defaults={"sort_order": order}
        )


def unseed(apps, schema_editor):
    RenewalType = apps.get_model("fleet", "RenewalType")
    for name, applies_to, _order in SEED:
        RenewalType.objects.filter(name=name, applies_to=applies_to, renewals__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("fleet", "0002_vehicle_renewals")]
    operations = [migrations.RunPython(seed, unseed)]
