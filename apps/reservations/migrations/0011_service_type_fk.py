"""Reservation.service (free text) becomes a FK to the ServiceType catalog.

Order matters: add the column, move the data, only then drop the old one. Every
distinct free-text value is preserved — matched to a catalog entry case-insensitively,
or created as an INACTIVE one. Inactive keeps a year of ad-hoc phrasings out of the
new dropdown while leaving them readable on the trips that used them and editable in
Settings, where the owner can rename, merge, or activate them.
"""

import django.db.models.deletion
from django.db import migrations, models


def link_service_types(apps, _schema_editor):
    Reservation = apps.get_model("reservations", "Reservation")
    ServiceType = apps.get_model("leads", "ServiceType")

    by_name = {st.name.casefold(): st for st in ServiceType.objects.all()}
    rows = Reservation.objects.exclude(service="").values_list("pk", "service")
    for pk, service in rows:
        name = (service or "").strip()
        if not name:
            continue
        existing = by_name.get(name.casefold())
        if existing is None:
            existing = ServiceType.objects.create(name=name[:120], active=False, sort_order=99)
            by_name[name.casefold()] = existing
        Reservation.objects.filter(pk=pk).update(service_type=existing)


def unlink_service_types(apps, _schema_editor):
    """Reverse: write the catalog name back into the free-text column."""
    Reservation = apps.get_model("reservations", "Reservation")
    for res in Reservation.objects.exclude(service_type=None).select_related("service_type"):
        Reservation.objects.filter(pk=res.pk).update(service=res.service_type.name)


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0008_service_type"),
        ("reservations", "0010_flight_verification"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservation",
            name="service_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="leads.servicetype",
            ),
        ),
        migrations.RunPython(link_service_types, unlink_service_types),
        migrations.RemoveField(
            model_name="reservation",
            name="service",
        ),
    ]
