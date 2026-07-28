"""One-time LocationIQ backfill for the airport directory.

Adds a street line, postal code and place id to each airport so a picked airport looks
like any other LocationIQ result downstream. Run manually once LOCATIONIQ_API_KEY is set —
this is deliberately not part of any deploy. Coordinates are never touched: the CSV's are
authoritative, and a forward search on an airport name routinely lands on a taxiway or a
cargo gate, which is exactly what the distance check below rejects.
"""

import time
from math import asin, cos, radians, sin, sqrt

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.addresses.models import Airport

SEARCH_URL = "https://us1.locationiq.com/v1/search"
TIMEOUT = 15
MAX_DISTANCE_MILES = 3.0
# LocationIQ's free tier allows 2 requests/second.
REQUEST_INTERVAL_SECONDS = 0.55
_EARTH_RADIUS_MILES = 3958.8


def _miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * asin(sqrt(a))


def _search(airport: Airport) -> list[dict]:
    response = requests.get(
        SEARCH_URL,
        params={
            "key": settings.LOCATIONIQ_API_KEY,
            "q": f"{airport.name}, {airport.city}, {airport.state}",
            "format": "json",
            "addressdetails": 1,
            "limit": 5,
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _pick(airport: Airport, candidates: list[dict]) -> dict | None:
    """The nearest candidate within MAX_DISTANCE_MILES, preferring aeroway features."""
    scored = []
    for item in candidates:
        try:
            distance = _miles(
                float(item["lat"]),
                float(item["lon"]),
                float(airport.latitude),
                float(airport.longitude),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= MAX_DISTANCE_MILES:
            scored.append((0 if item.get("class") == "aeroway" else 1, distance, item))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]))
    return scored[0][2]


def enrich_airport(airport: Airport) -> bool:
    """Populate the enrichment fields. Returns True on a hit, False on a miss.

    Either way `enriched_at` is stamped, so a rerun doesn't retry a known miss forever.
    """
    match = _pick(airport, _search(airport))
    if match is None:
        airport.enriched_at = timezone.now()
        airport.save(update_fields=["enriched_at", "updated_at"])
        return False

    address = match.get("address") or {}
    house = (address.get("house_number") or "").strip()
    road = (address.get("road") or "").strip()
    airport.line1 = f"{house} {road}".strip()[:200]
    airport.postal = (address.get("postcode") or "").strip()[:20]
    airport.locationiq_place_id = str(match.get("place_id") or "")[:64]
    airport.display_name = (match.get("display_name") or "")[:300]
    airport.enriched_at = timezone.now()
    airport.save(
        update_fields=[
            "line1",
            "postal",
            "locationiq_place_id",
            "display_name",
            "enriched_at",
            "updated_at",
        ]
    )
    return True


class Command(BaseCommand):
    help = "Backfill LocationIQ place data onto airports (one API call each, throttled)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Stop after N airports.")
        parser.add_argument(
            "--force", action="store_true", help="Re-process airports already enriched."
        )

    def handle(self, *args, **options):
        if not settings.LOCATIONIQ_API_KEY:
            raise CommandError("LOCATIONIQ_API_KEY is not set.")

        queryset = Airport.objects.all().order_by("pk")
        if not options["force"]:
            queryset = queryset.filter(enriched_at__isnull=True)
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        hits = misses = 0
        for index, airport in enumerate(queryset):
            if index:
                time.sleep(REQUEST_INTERVAL_SECONDS)
            if enrich_airport(airport):
                hits += 1
            else:
                misses += 1
                self.stdout.write(f"  no match within {MAX_DISTANCE_MILES} mi: {airport.label}")

        self.stdout.write(self.style.SUCCESS(f"Enriched {hits}, missed {misses}."))
