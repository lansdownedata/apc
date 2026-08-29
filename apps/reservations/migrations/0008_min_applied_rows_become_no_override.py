# Pricing moved from rate × max(hours, min_hours) to rate × (hours if hours > 0 else
# min_hours) — an override now REPLACES the minimum. A row where the minimum was in force
# under the old rule (0 < hours < min_hours) would silently bill the smaller number, so
# it becomes "no override" (hours = 0): billed_hours, line_total, min_applied and the
# derived drop-off are all unchanged by construction. Reverse is a no-op — which zeros
# were once sub-minimum overrides is not recoverable, and the totals are identical anyway.
import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)


def blank_sub_minimum_overrides(Reservation) -> int:
    return Reservation.objects.filter(hours__gt=0, hours__lt=F("min_hours")).update(hours=0)


def forwards(apps, schema_editor):
    count = blank_sub_minimum_overrides(apps.get_model("reservations", "Reservation"))
    logger.info("reservations: %d sub-minimum override(s) reset to the rate-card minimum", count)


class Migration(migrations.Migration):
    dependencies = [("reservations", "0007_remove_reservation_base_rate_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
