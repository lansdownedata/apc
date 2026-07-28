from django.core.management.base import BaseCommand

from apps.addresses.loaders import CSV_PATH, load_airports
from apps.addresses.models import Airport


class Command(BaseCommand):
    help = "Upsert the airport directory from the committed CSV (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--path", default=None, help=f"CSV to load (default: {CSV_PATH})")

    def handle(self, *args, **options):
        created, updated = load_airports(Airport, options["path"])
        self.stdout.write(self.style.SUCCESS(f"Airports: {created} created, {updated} updated."))
