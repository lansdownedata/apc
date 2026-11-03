from django.db import migrations
from django.db.models import F

from apps.reservations.passengers import split_names


def names_to_passengers(apps, schema_editor):
    """Copy each lead's free-text names onto its first trip (the field stays for now)."""
    Lead = apps.get_model("leads", "Lead")
    Reservation = apps.get_model("reservations", "Reservation")
    Passenger = apps.get_model("reservations", "Passenger")
    for lead in Lead.objects.exclude(passenger_names="").iterator():
        names = split_names(lead.passenger_names)
        if not names:
            continue
        first = (
            Reservation.objects.filter(lead=lead)
            .order_by(
                F("pickup_date").asc(nulls_last=True),
                F("pickup_time").asc(nulls_last=True),
                "sort_order",
                "pk",
            )
            .first()
        )
        if first is None:
            print(f"passenger_names dropped for lead {lead.pk}: no trips")
            continue
        Passenger.objects.bulk_create(
            [Passenger(reservation=first, name=n, sort=i) for i, n in enumerate(names)]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0028_passenger"),
        ("leads", "0019_lead_billing_group"),
    ]

    operations = [migrations.RunPython(names_to_passengers, migrations.RunPython.noop)]
