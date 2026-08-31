"""Fill blank Reservation.pickup_timezone from each pickup stop.

Not a data migration: timezonefinder must not be pinned into migration history.
Safe to re-run — only rows with a blank pickup_timezone are visited.
"""

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from apps.reservations.models import Reservation, Stop
from apps.reservations.timezones import resolve


class Command(BaseCommand):
    help = "Resolve blank pickup_timezone values from pickup airport or coordinates."

    def handle(self, *args, **options):
        qs = Reservation.objects.filter(pickup_timezone="").prefetch_related(
            Prefetch(
                "stops",
                queryset=Stop.objects.select_related("airport").order_by("sequence"),
            )
        )
        airport = coords = fallback = 0
        for res in qs:
            pickup = next(iter(res.stops.all()), None)
            zone = resolve(pickup) if pickup is not None else ""
            airport_tz = (
                pickup.airport.timezone
                if pickup is not None and pickup.airport_id and pickup.airport.timezone
                else ""
            )
            if airport_tz:
                airport += 1
            elif zone:
                coords += 1
            else:
                fallback += 1
            if zone:
                res.pickup_timezone = zone
                res.save(update_fields=["pickup_timezone"])
        self.stdout.write(f"airport={airport} coords={coords} fallback={fallback}")
