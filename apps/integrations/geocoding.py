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
