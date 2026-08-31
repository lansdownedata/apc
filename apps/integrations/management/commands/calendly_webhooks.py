from django.core.management.base import BaseCommand

from apps.integrations import calendly


class Command(BaseCommand):
    help = "List (or delete) this account's Calendly webhook subscriptions."

    def add_arguments(self, parser):
        parser.add_argument("--scope", default="organization", choices=["organization", "user"])
        parser.add_argument("--delete", default="", help="UUID of a subscription to delete")

    def handle(self, *args, **options):
        try:
            me = calendly.current_user()
        except (calendly.CalendlyNotConfigured, calendly.CalendlyAPIError) as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        if options["delete"]:
            try:
                calendly.delete_webhook(options["delete"])
            except calendly.CalendlyAPIError as exc:
                self.stderr.write(self.style.ERROR(str(exc)))
                return
            self.stdout.write(self.style.SUCCESS(f"Deleted {options['delete']}."))
            return

        try:
            result = calendly.list_webhooks(
                organization=me.get("current_organization", ""),
                scope=options["scope"],
                user=me.get("uri", ""),
            )
        except calendly.CalendlyAPIError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        rows = result.get("collection", [])
        if not rows:
            self.stdout.write("No webhook subscriptions.")
            return
        for row in rows:
            self.stdout.write(
                f"{row.get('uri', '').rsplit('/', 1)[-1]}  {row.get('state')}  "
                f"{row.get('callback_url')}  {', '.join(row.get('events') or [])}"
            )
            # Calendly retries a failing endpoint for 24h and then DISABLES the
            # subscription, which can only be fixed by creating a new one. This
            # timestamp is the only warning that the clock is running.
            if row.get("retry_started_at"):
                self.stdout.write(
                    self.style.WARNING(
                        f"    ⚠ RETRYING since {row['retry_started_at']} — deliveries are "
                        "failing; the subscription is disabled 24h after this."
                    )
                )
