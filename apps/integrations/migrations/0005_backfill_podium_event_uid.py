from django.db import migrations


def forwards(apps, schema_editor):
    """Stamp event_uid on historical rows, first-seen wins.

    Production already contains retry duplicates (one message.failed arrived 8 times on
    2026-07-31), so stamping every row would violate the unique index. The earliest row
    for each uid gets it; later duplicates keep NULL. Deliberately non-destructive — the
    duplicate rows stay as a record of what happened rather than being deleted to tidy a log.
    """
    PodiumEvent = apps.get_model("integrations", "PodiumEvent")
    claimed: set[str] = set()
    for event in PodiumEvent.objects.order_by("id").iterator():
        uid = ((event.payload or {}).get("metadata") or {}).get("eventUid")
        if not uid or uid in claimed:
            continue
        claimed.add(uid)
        PodiumEvent.objects.filter(pk=event.pk).update(event_uid=uid)


def backwards(apps, schema_editor):
    PodiumEvent = apps.get_model("integrations", "PodiumEvent")
    PodiumEvent.objects.update(event_uid=None)


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0004_podiumevent_event_uid"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
