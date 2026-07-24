"""Forward geocoding via LocationIQ — LA requires lat/lng on every address."""

from decimal import Decimal
from typing import TYPE_CHECKING

import requests
from django.conf import settings

if TYPE_CHECKING:
    from apps.reservations.models import Stop

SEARCH_URL = "https://us1.locationiq.com/v1/search"
TIMEOUT = 15


class GeocodeError(Exception):
    """Address could not be geocoded (missing key, empty address, or no results)."""


def geocode(address: str) -> tuple[Decimal, Decimal]:
    if not settings.LOCATIONIQ_API_KEY:
        raise GeocodeError("LOCATIONIQ_API_KEY is not set.")
    address = (address or "").strip()
    if not address:
        raise GeocodeError("Empty address.")
    resp = requests.get(
        SEARCH_URL,
        params={
            "key": settings.LOCATIONIQ_API_KEY,
            "q": address,
            "format": "json",
            "limit": 1,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise GeocodeError(f"LocationIQ {resp.status_code}: {(resp.text or '')[:300]}")
    results = resp.json()
    if not results:
        raise GeocodeError(f"No geocoding results for {address!r}.")
    return Decimal(results[0]["lat"]), Decimal(results[0]["lon"])


def geocode_stop(stop: "Stop") -> tuple[Decimal, Decimal]:
    """Geocode a Stop, caching coordinates on the row (one LocationIQ hit per address)."""
    if stop.latitude is not None and stop.longitude is not None:
        return stop.latitude, stop.longitude
    lat, lng = geocode(stop.address)
    stop.latitude, stop.longitude = lat, lng
    stop.save(update_fields=["latitude", "longitude", "updated_at"])
    return lat, lng


AUTOCOMPLETE_URL = "https://api.locationiq.com/v1/autocomplete"


def _decompose(item: dict) -> dict:
    """Map a LocationIQ autocomplete result to the Address field set. Defensive (.get) —
    the response shape is VERIFY-LIVE (probe against the real API before trusting field names)."""
    addr = item.get("address") or {}
    house = (addr.get("house_number") or "").strip()
    road = (addr.get("road") or "").strip()
    street = f"{house} {road}".strip()
    name = (addr.get("name") or "").strip()  # POI name, if any
    return {
        "landmark_name": name if (name and (road or item.get("type") != "house")) else "",
        "line1": street,
        "line2": "",
        "city": addr.get("city") or addr.get("town") or addr.get("village") or "",
        "state": addr.get("state") or "",
        "postal": addr.get("postcode") or "",
        "country": addr.get("country") or "",
        "latitude": item.get("lat") or None,
        "longitude": item.get("lon") or None,
        "place_id": str(item.get("place_id") or ""),
        "place_type": item.get("type") or "",
        "place_class": item.get("class") or "",
        "display_name": item.get("display_name") or "",
    }


def autocomplete(q: str) -> list[dict]:
    """Type-ahead address search via LocationIQ, decomposed to the Address field set.
    Returns [] when the key is missing, the query is blank, or the API errors — never raises."""
    q = (q or "").strip()
    if not q or not settings.LOCATIONIQ_API_KEY:
        return []
    try:
        resp = requests.get(
            AUTOCOMPLETE_URL,
            params={"key": settings.LOCATIONIQ_API_KEY, "q": q, "limit": 6, "dedupe": 1},
            timeout=TIMEOUT,
        )
        if resp.status_code >= 400:
            return []
        payload = resp.json()
        if not isinstance(payload, list):
            return []
        return [_decompose(item) for item in payload if isinstance(item, dict)]
    except (requests.RequestException, ValueError):
        return []
