"""One-off live probe of aviationstack — run by hand with a real key, never from tests.

    manage.py aviationstack_probe --airline UA --flight 123 --airport IAD --date 2026-10-15
    manage.py aviationstack_probe ... --direction departure
    manage.py aviationstack_probe ... --endpoint timetable    # force /v1/timetable

Prints the request (key redacted), the HTTP status and the raw JSON, and writes the body to
docs/aviationstack/probes/<date>-<endpoint>-<flight>-<direction>.json so real responses become
the client's test fixtures. Ground truth from the 2026-08-29 probe on the real key
(task-3R-brief.md), not the vendor's docs: `/v1/flights` is 403 `function_access_restricted`
on this subscription and is never called; `/v1/flightsFuture` only serves dates more than
`FUTURE_AFTER_DAYS` out (a 500 inside that window); `/v1/timetable` is the day-of source, and
takes no `date` param at all — it's filtered server-side by `iataCode` + `type` +
`flight_iata` only. `auto` routes accordingly, so days 1-7 out (no live coverage on this plan)
still resolve to a real call, just not a useful one — that gap is `LIVE_LOOKAHEAD_DAYS = 0` in
apps/reservations/flights.py, not something this probe can route around.
"""

import json
from datetime import date
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

PROBE_DIR = Path(settings.BASE_DIR) / "docs" / "aviationstack" / "probes"
TIMEOUT = 15
FUTURE_AFTER_DAYS = 7  # flightsFuture only serves dates more than this many days out


def today() -> date:
    return date.today()


class Command(BaseCommand):
    help = "Hit aviationstack once with the real key; print and save the raw response."

    def add_arguments(self, parser):
        parser.add_argument("--airline", required=True, help="Carrier IATA code, e.g. UA")
        parser.add_argument("--flight", required=True, help="Flight number digits, e.g. 123")
        parser.add_argument("--airport", required=True, help="Airport IATA code, e.g. IAD")
        parser.add_argument("--direction", choices=["arrival", "departure"], default="arrival")
        parser.add_argument("--date", required=True, help="YYYY-MM-DD, local date at the airport")
        parser.add_argument(
            "--endpoint", choices=["auto", "flightsFuture", "timetable"], default="auto"
        )

    def handle(self, *args, **opts):
        key = settings.AVIATIONSTACK_API_KEY
        if not key:
            raise CommandError("AVIATIONSTACK_API_KEY is not set — add it to .env first.")
        day = date.fromisoformat(opts["date"])
        endpoint = opts["endpoint"]
        if endpoint == "auto":
            far = (day - today()).days > FUTURE_AFTER_DAYS
            endpoint = "flightsFuture" if far else "timetable"
        if endpoint == "flightsFuture":
            params = {
                "iataCode": opts["airport"].upper(),
                "type": opts["direction"],
                "date": day.isoformat(),
                "airline_iata": opts["airline"].upper(),
                "flight_number": opts["flight"],
            }
        else:
            params = {
                "iataCode": opts["airport"].upper(),
                "type": opts["direction"],
                "flight_iata": f"{opts['airline'].upper()}{opts['flight']}",
            }
        url = f"{settings.AVIATIONSTACK_BASE_URL}/v1/{endpoint}"
        self.stdout.write(f"GET {url} {json.dumps(params)}  (key redacted)")
        resp = requests.get(url, params={**params, "access_key": key}, timeout=TIMEOUT)
        self.stdout.write(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            self.stdout.write((resp.text or "")[:2000])
            raise CommandError("Response was not JSON.") from None
        text = json.dumps(body, indent=2)
        self.stdout.write(text[:6000])
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{day.isoformat()}-{endpoint}-{opts['airline'].upper()}"
        name += f"{opts['flight']}-{opts['direction']}.json"
        out = PROBE_DIR / name
        out.write_text(text + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Saved {out}"))
