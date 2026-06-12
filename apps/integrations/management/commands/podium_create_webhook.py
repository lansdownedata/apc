import secrets

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.integrations import podium

EVENT_TYPES = ["message.received", "message.sent", "message.failed"]


class Command(BaseCommand):
    help = "Register a Podium message webhook pointing at this app (uses the stored token)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="",
            help="Webhook URL (defaults to https://<NGROK_HOST>/webhooks/podium/)",
        )
        parser.add_argument("--secret", default="", help="Signing secret (generated if omitted)")

    def handle(self, *args, **options):
        url = options["url"]
        if not url:
            host = getattr(settings, "NGROK_HOST", "")
            if not host:
                self.stderr.write(self.style.ERROR("Pass --url (NGROK_HOST is not set)."))
                return
            url = f"https://{host}/webhooks/podium/"

        secret = options["secret"] or settings.PODIUM_WEBHOOK_SECRET or secrets.token_urlsafe(24)

        try:
            result = podium.create_webhook(
                url=url,
                event_types=EVENT_TYPES,
                secret=secret,
                organization_uid=settings.PODIUM_ORGANIZATION_UID or None,
                location_uid=settings.PODIUM_LOCATION_UID or None,
            )
        except podium.PodiumNotConnected as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        self.stdout.write(self.style.SUCCESS(f"Webhook created: {result}"))
        self.stdout.write(f"  URL:    {url}")
        self.stdout.write(f"  Events: {', '.join(EVENT_TYPES)}")
        self.stdout.write(self.style.WARNING(f"\nAdd to .env →  PODIUM_WEBHOOK_SECRET={secret}"))
