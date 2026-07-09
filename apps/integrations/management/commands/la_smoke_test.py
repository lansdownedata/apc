"""Credential-day live check: walk the LA API end to end and print each step.

Run once real LA credentials land in .env:
    python manage.py la_smoke_test --email throwaway+apc1@example.com
"""

import json
import secrets

from django.core.management.base import BaseCommand

from apps.integrations import limoanywhere as la


class Command(BaseCommand):
    help = "Verify LimoAnywhere API credentials by walking the documented booking flow."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", required=True, help="Throwaway email for the test customer registration."
        )

    def handle(self, *args, **options):
        if not la.is_configured():
            self.stdout.write(
                self.style.ERROR(
                    "LA is not configured — set LA_CLIENT_ID/LA_CLIENT_SECRET/LA_COMPANY_ALIAS."
                )
            )
            raise SystemExit(1)

        def step(label: str, fn):
            self.stdout.write(f"→ {label} … ", ending="")
            result = fn()
            self.stdout.write(self.style.SUCCESS("ok"))
            if result:
                self.stdout.write(json.dumps(result, indent=2)[:1500])
            return result

        step("token (client credentials)", lambda: {"token": la.get_token()[:12] + "…"})
        step("payment types (pick LA_PAYMENT_TYPE_ID from this!)", la.list_payment_types)
        step("service types", la.list_service_types)
        step("vehicle types", la.list_vehicle_types)
        email = options["email"]
        step("validate email", lambda: la.validate_email(email))
        password = secrets.token_urlsafe(16)
        step(
            "register test customer",
            lambda: la.register_customer(
                {
                    "first_name": "APC",
                    "last_name": "Smoketest",
                    "email": email,
                    "password": password,
                }
            ),
        )
        token = la.get_token(username=email, password=password)
        self.stdout.write(self.style.SUCCESS("→ password grant … ok"))
        rate = step(
            "rate lookup (watch for empty results = rate matrix unconfigured)",
            lambda: la.rate_lookup(
                {
                    "result_type": "Mixed",
                    "passenger_count": 2,
                    "scheduled_pickup_at": "2027-01-15T15:00:00Z",
                    "pickup": {
                        "address": {
                            "address_line1": "JFK Airport",
                            "city": "Queens",
                            "state_code": "NY",
                            "postal_code": "11430",
                            "country_code": "US",
                            "latitude": 40.6413,
                            "longitude": -73.7781,
                        }
                    },
                    "dropoff": {
                        "address": {
                            "address_line1": "Times Square",
                            "city": "New York",
                            "state_code": "NY",
                            "postal_code": "10036",
                            "country_code": "US",
                            "latitude": 40.7580,
                            "longitude": -73.9855,
                        }
                    },
                },
                token=token,
            ),
        )
        results = rate.get("results") or []
        if not results:
            self.stdout.write(
                self.style.WARNING(
                    "rate_lookup returned NO results — add a $0 rate per vehicle type in LA, "
                    "then re-run."
                )
            )
            raise SystemExit(1)
        booking = step(
            "create test booking",
            lambda: la.create_booking(
                {
                    "search_result_id": results[0]["id"],
                    "passengers": [{"first_name": "APC", "last_name": "Smoketest"}],
                    "payment_type_id": 1,
                },
                token=token,
            ),
        )
        step("cancel test booking", lambda: la.cancel_reservation(str(booking["id"]), token=token))
        step(
            "webhook subscribe (training)",
            lambda: (
                la.subscribe_webhook("https://example.com/webhooks/limoanywhere/test/", token=token)
                or {}
            ),
        )
        self.stdout.write(self.style.SUCCESS("All steps passed — LA integration is live-ready."))
        self.stdout.write(
            "Remember: delete the test customer in LA admin, and set "
            "LA_PAYMENT_TYPE_ID in .env from the payment-types list above."
        )
