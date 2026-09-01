from django.core.management.base import BaseCommand, CommandError

from apps.addresses.loaders import VENUES_CSV_PATH, load_venue_caps, load_venues
from apps.addresses.models import Venue


class Command(BaseCommand):
    help = "Upsert the wedding venue / hotel / ceremony-site directory from the CSV (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", default=None, help=f"CSV to load (default: {VENUES_CSV_PATH})"
        )
        parser.add_argument(
            "--caps",
            action="store_true",
            help=(
                "Bulk-update vehicle_cap/cap_note on EXISTING venues only, keyed on "
                "(name, kind). CSV columns: name,kind,vehicle_cap,cap_note. Requires --path; "
                "does not create rows or touch any other field."
            ),
        )

    def handle(self, *args, **options):
        if options["caps"]:
            if not options["path"]:
                raise CommandError("--caps requires --path pointing at a caps CSV")
            updated, unmatched = load_venue_caps(Venue, options["path"])
            self.stdout.write(self.style.SUCCESS(f"Venue caps: {updated} updated."))
            if unmatched:
                rows = ", ".join(f"{name} ({kind})" for name, kind in unmatched)
                self.stdout.write(
                    self.style.WARNING(f"{len(unmatched)} row(s) with no directory match: {rows}")
                )
            return

        created, updated = load_venues(Venue, options["path"])
        self.stdout.write(self.style.SUCCESS(f"Venues: {created} created, {updated} updated."))
