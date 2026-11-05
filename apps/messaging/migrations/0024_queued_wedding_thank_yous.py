"""The cut-over to one messaging model (APC-118): a thank-you the old engine queued on a
wedding order sends the wedding wording, as the old engine would have.

The old engine queued one `thank_you` for every finished order and picked the wedding words
at send time. The new sender sends the row a TouchPoint points at, so a queued `thank_you` on
a wedding order is pointed at the `thank_you_wedding` row here. Its kind stays `thank_you`,
which is what stops the wedding row being queued a second time for the same order.

"Wedding order" is the test the wedding row's rules use: an order with a wedding name, or
one with a live trip of the row's service types (the Wedding type matching legacy
wedding-builder legs too). Nothing is moved while the wedding row is off, archived or moved
to another trigger: then the plain thank-you is what that order gets.
"""

from django.db import migrations
from django.db.models import Q

WEDDING_SERVICE_NAME = "Wedding Transportation"
# The cancelled trip statuses, frozen (`dispatch.selectors.CANCELLED_STATUSES`).
CANCELLED = ("cancelled", "cancelled_by_affiliate", "late_cancel", "no_show", "covid_cancellation")


def _is_wedding_type(service_type) -> bool:
    return service_type.name.strip().lower() == WEDDING_SERVICE_NAME.lower()


def _trip_matches(trip, types) -> bool:
    for st in types:
        if _is_wedding_type(st):
            if trip.source_leg_id or (trip.service_type and _is_wedding_type(trip.service_type)):
                return True
        elif trip.service_type_id == st.pk:
            return True
    return False


def repoint(apps, schema_editor):
    MessageTemplate = apps.get_model("messaging", "MessageTemplate")
    TouchPoint = apps.get_model("messaging", "TouchPoint")
    Lead = apps.get_model("leads", "Lead")
    Reservation = apps.get_model("reservations", "Reservation")

    wedding = MessageTemplate.objects.filter(
        key="thank_you_wedding",
        seeded=True,
        trigger="all_trips_done",
        enabled=True,
        archived_at__isnull=True,
    ).first()
    if wedding is None:
        return
    queued = TouchPoint.objects.filter(kind="thank_you", status="scheduled").filter(
        Q(message__isnull=True) | Q(message__key="thank_you")
    )
    lead_ids = set(queued.values_list("lead_id", flat=True))
    if not lead_ids:
        return
    types = list(wedding.service_types.all())
    if types:
        weddings = set(
            Lead.objects.filter(pk__in=lead_ids).exclude(wedding_name="").values_list("pk", flat=True)
        )
        trips = (
            Reservation.objects.filter(lead_id__in=lead_ids)
            .exclude(trip_status__in=CANCELLED)
            .select_related("service_type")
        )
        weddings |= {t.lead_id for t in trips if _trip_matches(t, types)}
    else:
        weddings = lead_ids  # a row with no service types matches every order
    queued.filter(lead_id__in=weddings).update(message=wedding)


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0020_remove_lead_passenger_names"),
        ("reservations", "0029_passengers_from_names"),
        ("messaging", "0023_message_rows_required"),
    ]

    operations = [migrations.RunPython(repoint, migrations.RunPython.noop)]
