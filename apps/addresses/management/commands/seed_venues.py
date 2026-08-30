from django.core.management.base import BaseCommand

from apps.addresses.loaders import VENUES_CSV_PATH, load_venues
from apps.addresses.models import Venue


class Command(BaseCommand):
    help = "Upsert the wedding venue / hotel / ceremony-site directory from the CSV (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", default=None, help=f"CSV to load (default: {VENUES_CSV_PATH})"
        )

    def handle(self, *args, **options):
        created, updated = load_venues(Venue, options["path"])
        self.stdout.write(self.style.SUCCESS(f"Venues: {created} created, {updated} updated."))
