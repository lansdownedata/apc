from django.db import migrations


def backfill(apps, schema_editor):
    """Put every trip saved at factor 0 on the Settings standard.

    The standard used to be a browser pre-fill, so any trip made outside the editor (and
    every trip from before 2026-09-05) stored 0 and paid its vendor nothing on paper.
    `Reservation.save` fills it from now on; this catches the rows already there. Trips
    with a factor of their own are left exactly as they are.
    """
    PricingConfig = apps.get_model("reservations", "PricingConfig")
    Reservation = apps.get_model("reservations", "Reservation")
    config = PricingConfig.objects.filter(pk=1).first()
    standard = config.default_cost_ratio_pct if config else 65
    Reservation.objects.filter(cost_ratio_pct=0).update(cost_ratio_pct=standard)


class Migration(migrations.Migration):
    dependencies = [("reservations", "0018_reservation_dropoff_estimated")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
