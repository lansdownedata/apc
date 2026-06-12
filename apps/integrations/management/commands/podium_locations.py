from django.core.management.base import BaseCommand

from apps.integrations import podium


class Command(BaseCommand):
    help = "List Podium locations for the connected org (to find the location UID)."

    def handle(self, *args, **options):
        try:
            data = podium.list_locations()
        except podium.PodiumNotConnected as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        locations = data.get("data", data) if isinstance(data, dict) else data
        if not locations:
            self.stdout.write("No locations returned.")
            return

        self.stdout.write(self.style.SUCCESS(f"{len(locations)} location(s):"))
        for loc in locations:
            if isinstance(loc, dict):
                self.stdout.write(f"  {loc.get('uid', '?')}  {loc.get('name', '')}")
            else:
                self.stdout.write(f"  {loc}")
        self.stdout.write("\nSet the right one as PODIUM_LOCATION_UID in .env.")
