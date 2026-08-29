from django.core.management.base import BaseCommand

from apps.addresses.loaders import AIRLINES_CSV_PATH, load_airlines
from apps.addresses.models import Airline


class Command(BaseCommand):
    help = "Upsert the airline directory from the committed CSV (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", default=None, help=f"CSV to load (default: {AIRLINES_CSV_PATH})"
        )

    def handle(self, *args, **options):
        created, updated = load_airlines(Airline, options["path"])
        self.stdout.write(self.style.SUCCESS(f"Airlines: {created} created, {updated} updated."))
