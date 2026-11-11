"""Trip status cleanup (spec 2026-11-10, "Data migration"): move every reservation off the
eight retired statuses, and off a blank one, onto the 11 that remain, then shrink the
choices.

One-way: the reverse is a no-op. The facts a retired value carried move to their own
fields (`needs_farm_out`, the circling notice, `cancel_covid`), so the old value can't be
told apart afterwards. Restore from a snapshot to go back.

`TripStatusEvent` rows keep the raw value they were written with (History labels them
from `RETIRED_STATUS_LABELS`); this migration writes none.
"""

from django.db import migrations, models

BOOKED = "booked"
OFFERED = "offered"
CONFIRMED = "confirmed"
COVID_REASON = "COVID-19 (migrated)"
CIRCLING_SOURCE = "limoanywhere"
# Every value the migration reads: blank, "assigned" (a survivor whose meaning moved: an
# accepted offer is Dispatched now) and the eight retired ones.
OLD_VALUES = (
    "",
    "assigned",
    "farm_out_unassigned",
    "offered",
    "offered_to_affiliate",
    "affiliate_assigned",
    "dispatched_non_la",
    "circling",
    "cancelled_by_affiliate",
    "covid_cancellation",
)


def _active(Assignment, trip) -> str:
    """The status of the trip's active coverage: confirmed, offered, or "" for none."""
    statuses = set(
        Assignment.objects.filter(reservation=trip, status__in=(OFFERED, CONFIRMED)).values_list(
            "status", flat=True
        )
    )
    if CONFIRMED in statuses:
        return CONFIRMED
    return OFFERED if OFFERED in statuses else ""


def _new_values(trip, Assignment, TripStatusEvent, now) -> dict:
    """The fields to write for one trip, keyed by field name (status included)."""
    old = trip.trip_status
    if old == "":
        if trip.lead.status != BOOKED:
            return {"trip_status": "pending"}
        by_coverage = {CONFIRMED: "dispatched", OFFERED: "assigned", "": "unassigned"}
        return {"trip_status": by_coverage[_active(Assignment, trip)]}
    if old in ("farm_out_unassigned", "cancelled_by_affiliate"):
        return {"trip_status": "unassigned", "needs_farm_out": True}
    if old in ("offered", "offered_to_affiliate"):
        return {"trip_status": "assigned"}
    if old in ("assigned", "affiliate_assigned"):
        confirmed = _active(Assignment, trip) == CONFIRMED
        return {"trip_status": "dispatched" if confirmed else "assigned"}
    if old == "dispatched_non_la":
        return {"trip_status": "dispatched"}
    if old == "circling":
        event = (
            TripStatusEvent.objects.filter(reservation=trip, status="circling")
            .order_by("-created_at", "-pk")
            .first()
        )
        return {
            "trip_status": "arrived",
            "circling_at": event.created_at if event else now,
            "circling_source": CIRCLING_SOURCE,
        }
    # covid_cancellation
    return {"trip_status": "cancelled", "cancel_covid": True, "cancel_reason": COVID_REASON}


def remap(Reservation, Assignment, TripStatusEvent, now) -> dict[str, int]:
    """Rewrite every reservation holding an old value; returns how many held each one.

    Per-row `save(update_fields=...)` (APC-140: no bulk writes on audited models). A row
    whose value doesn't change (Assigned with no confirmed coverage) is not saved.
    """
    counts = dict.fromkeys(OLD_VALUES, 0)
    trips = Reservation.objects.filter(trip_status__in=OLD_VALUES).select_related("lead")
    for trip in trips.order_by("pk"):
        counts[trip.trip_status] += 1
        values = _new_values(trip, Assignment, TripStatusEvent, now)
        changed = [name for name, value in values.items() if getattr(trip, name) != value]
        if not changed:
            continue
        for name in changed:
            setattr(trip, name, values[name])
        trip.save(update_fields=[*changed, "updated_at"])
    return counts


def forwards(apps, schema_editor) -> None:
    from django.utils import timezone

    counts = remap(
        apps.get_model("reservations", "Reservation"),
        apps.get_model("dispatch", "Assignment"),
        apps.get_model("reservations", "TripStatusEvent"),
        timezone.now(),
    )
    moved = {old or "(blank)": n for old, n in counts.items() if n}
    if moved:
        print(f"\n  Remapped trip statuses: {moved}")


_CHOICES = [
    ("pending", "Pending"),
    ("unassigned", "Unassigned"),
    ("assigned", "Assigned"),
    ("dispatched", "Dispatched"),
    ("on_the_way", "On The Way"),
    ("arrived", "Arrived"),
    ("customer_in_car", "Customer In Car"),
    ("done", "Done"),
    ("cancelled", "Cancelled"),
    ("late_cancel", "Late Cancel"),
    ("no_show", "No Show"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("reservations", "0034_trip_status_default_pending"),
        ("dispatch", "0015_assignment_sent_back"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="reservation",
            name="trip_status",
            field=models.CharField(blank=True, choices=_CHOICES, default="pending", max_length=32),
        ),
        migrations.AlterField(
            model_name="tripstatusevent",
            name="status",
            field=models.CharField(choices=_CHOICES, max_length=32),
        ),
    ]
