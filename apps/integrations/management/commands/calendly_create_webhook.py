import secrets

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.integrations import calendly


class Command(BaseCommand):
    help = "Register the Calendly webhook pointing at this app (there is no UI for this)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="",
            help="Webhook URL (defaults to PUBLIC_BASE_URL + /webhooks/calendly/)",
        )
        parser.add_argument("--scope", default="organization", choices=["organization", "user"])
        parser.add_argument("--signing-key", default="", help="HMAC key (generated if omitted)")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Register even though a subscription already points at this URL",
        )

    def handle(self, *args, **options):
        url = options["url"] or calendly.webhook_url()
        if not url:
            self.stderr.write(
                self.style.ERROR("Pass --url (neither PUBLIC_BASE_URL nor NGROK_HOST is set).")
            )
            return
        # Checked before any API call: Calendly requires https, and prod's
        # SECURE_SSL_REDIRECT would 301 an http delivery — which counts as a failure
        # and starts the 24h clock that ends in the subscription being disabled.
        if not url.startswith("https://"):
            self.stderr.write(self.style.ERROR(f"Webhook URL must be https, got {url!r}."))
            return

        try:
            me = calendly.current_user()
        except (calendly.CalendlyNotConfigured, calendly.CalendlyAPIError) as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        organization = me.get("current_organization", "")
        # Registering twice creates a DUPLICATE subscription, and every booking is then
        # delivered twice. The unique key downstream de-dupes the Lead, but the noise is
        # real and deleting a stray subscription needs its UUID — so refuse by default.
        try:
            existing = calendly.list_webhooks(
                organization=organization, scope=options["scope"], user=me.get("uri", "")
            ).get("collection", [])
        except calendly.CalendlyAPIError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            return

        clash = [row for row in existing if row.get("callback_url") == url]
        if clash and not options["force"]:
            self.stderr.write(
                self.style.ERROR(
                    f"A subscription already points at {url}. "
                    "Run `manage.py calendly_webhooks` to inspect it, or pass --force."
                )
            )
            return

        signing_key = (
            options["signing_key"]
            or settings.CALENDLY_WEBHOOK_SIGNING_KEY
            or secrets.token_urlsafe(24)
        )
        try:
            result = calendly.create_webhook(
                url=url,
                organization=organization,
                signing_key=signing_key,
                scope=options["scope"],
                user=me.get("uri", ""),
            )
        except calendly.CalendlyAPIError as exc:
            self.stderr.write(self.style.ERROR(f"Calendly API error: {exc}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Webhook created: {result}"))
        self.stdout.write(f"  URL:    {url}")
        self.stdout.write(f"  Events: {', '.join(calendly.WEBHOOK_EVENTS)}")
        if signing_key != settings.CALENDLY_WEBHOOK_SIGNING_KEY:
            self.stdout.write(
                self.style.WARNING(
                    f"\nSet this BEFORE the first booking or verification fails →  "
                    f"CALENDLY_WEBHOOK_SIGNING_KEY={signing_key}"
                )
            )
