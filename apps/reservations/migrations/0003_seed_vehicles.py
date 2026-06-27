from django.db import migrations

VEHICLES = [
    ("Luxury Sedan", 3, "sedan"),
    ("Luxury SUV", 6, "suv"),
    ("Sprinter Van", 14, "van"),
    ("Mini Coach", 24, "mini_coach"),
    ("Motor Coach", 56, "coach"),
    ("Stretch Limousine", 10, "limo"),
]


def seed(apps, schema_editor):
    Vehicle = apps.get_model("leads", "Vehicle")
    for name, capacity, klass in VEHICLES:
        Vehicle.objects.get_or_create(
            name=name, defaults={"capacity": capacity, "klass": klass, "active": True}
        )


def unseed(apps, schema_editor):
    Vehicle = apps.get_model("leads", "Vehicle")
    Vehicle.objects.filter(name__in=[v[0] for v in VEHICLES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0002_reservation_recognized_amount_and_more"),
        ("leads", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
