"""Forward geocoding via LocationIQ — LA requires lat/lng on every address."""

import hashlib
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

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

# Typeahead answers for the same prefix repeat constantly — one visitor backspacing, and
# every visitor who types "hilton garden inn". Cached briefly, that is both the cheapest
# request we can make and the one least likely to be rate-limited. Short enough that a
# newly curated place shows up the same afternoon.
AUTOCOMPLETE_CACHE_SECONDS = 15 * 60

# LocationIQ `class` values that are NOT a point of interest — a road, a locality/neighbourhood,
# or an administrative area. For these, `address.name` is just the street/place name, not a venue.
_NON_POI_CLASSES = {"highway", "place", "boundary"}


def _decompose(item: dict) -> dict:
    """Map a LocationIQ autocomplete result to the Address field set. Defensive (.get) —
    the response shape is VERIFY-LIVE (probe against the real API before trusting field names)."""
    addr = item.get("address") or {}
    house = (addr.get("house_number") or "").strip()
    road = (addr.get("road") or "").strip()
    street = f"{house} {road}".strip()
    name = (addr.get("name") or "").strip()  # POI name, if any
    is_poi = item.get("class") not in _NON_POI_CLASSES
    state_code = (addr.get("state_code") or "").strip()
    country_code = (addr.get("country_code") or "").strip()
    return {
        "landmark_name": name if (name and is_poi) else "",
        "line1": street,
        "line2": "",
        "city": addr.get("city") or addr.get("town") or addr.get("village") or "",
        "state": state_code.upper() if state_code else (addr.get("state") or ""),
        "postal": addr.get("postcode") or "",
        "country": country_code.upper() if country_code else (addr.get("country") or ""),
        "latitude": item.get("lat") or None,
        "longitude": item.get("lon") or None,
        "place_id": str(item.get("place_id") or ""),
        "place_type": item.get("type") or "",
        "place_class": item.get("class") or "",
        "display_name": item.get("display_name") or "",
    }


def autocomplete(q: str, lat=None, lon=None) -> list[dict]:
    """Type-ahead address search via LocationIQ, decomposed to the Address field set.
    A soft viewbox around (lat, lon) biases nearby results without restricting — never
    `bounded`/`countrycodes`. Returns [] when key/query missing or the API errors — never raises."""
    q = (q or "").strip()
    if not q or not settings.LOCATIONIQ_API_KEY:
        return []
    params = {
        "key": settings.LOCATIONIQ_API_KEY,
        "q": q,
        "limit": 20,
        "dedupe": 1,
        "normalizecity": 1,
    }
    try:
        clat, clon = float(lat), float(lon)
    except (TypeError, ValueError):
        clat = clon = None
    if clat is not None and clon is not None:
        r = settings.ADDRESS_BIAS_RADIUS_DEG
        # corners: (min_lon, max_lat, max_lon, min_lat) — a soft bias (no `bounded`)
        params["viewbox"] = (
            f"{round(clon - r, 4)},{round(clat + r, 4)},{round(clon + r, 4)},{round(clat - r, 4)}"
        )
    key = _autocomplete_cache_key(params)
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        resp = requests.get(AUTOCOMPLETE_URL, params=params, timeout=TIMEOUT)
        if resp.status_code >= 400:
            # NOT cached, and loud about 429 in particular: LocationIQ rate-limits from
            # the second request inside a second (probed 2026-09-06), which a typist can
            # outrun. Storing this empty list would hide a real place for the whole TTL,
            # and returning it silently is what made the limit look like "no such hotel".
            logger.warning(
                "LocationIQ autocomplete %s for %r: %s",
                resp.status_code,
                q,
                (resp.text or "")[:200],
            )
            return []
        payload = resp.json()
        if not isinstance(payload, list):
            return []
        rows = [_decompose(item) for item in payload if isinstance(item, dict)]
    except (requests.RequestException, ValueError):
        return []
    cache.set(key, rows, AUTOCOMPLETE_CACHE_SECONDS)
    return rows


def _autocomplete_cache_key(params: dict) -> str:
    """Hashed over the full parameter set so a viewbox change is a different entry."""
    raw = "|".join(f"{k}={params[k]}" for k in sorted(params) if k != "key")
    return f"liq-ac:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


# A LocationIQ aeroway result this close to an airport we already emitted is the same
# place seen twice — drop it rather than show the user a duplicate.
AIRPORT_DEDUPE_MILES = 0.5
_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * asin(sqrt(a))


def _duplicates_an_airport(result: dict, airports: list[dict]) -> bool:
    if result.get("place_class") != "aeroway":
        return False
    try:
        lat, lon = float(result["latitude"]), float(result["longitude"])
    except (KeyError, TypeError, ValueError):
        return False
    for airport in airports:
        distance = haversine_miles(
            lat, lon, float(airport["latitude"]), float(airport["longitude"])
        )
        if distance <= AIRPORT_DEDUPE_MILES:
            return True
    return False


def merged_autocomplete(q: str, lat=None, lon=None) -> list[dict]:
    """Airport matches first, then LocationIQ results with any airport twins removed.

    Airports are a local query, so this still returns results when LOCATIONIQ_API_KEY is
    unset — `autocomplete()` just contributes an empty list.
    """
    from apps.addresses.search import search_airports

    airports = search_airports(q)
    results = autocomplete(q, lat=lat, lon=lon)
    if airports:
        results = [r for r in results if not _duplicates_an_airport(r, airports)]
    return airports + results


# --- drive time (LocationIQ Directions) ------------------------------------------------

DIRECTIONS_URL = "https://us1.locationiq.com/v1/directions/driving"
# A route between two fixed points barely changes; the cost of a stale answer is a
# drop-off estimate a few minutes out, so this is cached hard.
DIRECTIONS_CACHE_SECONDS = 7 * 24 * 60 * 60
# ~11 m of precision. Enough that two stops at the same address share a cache entry
# without ever merging genuinely different addresses.
_COORD_PRECISION = 4


def drive_seconds(from_lat, from_lon, to_lat, to_lon) -> int | None:
    """Driving time between two points in seconds, or None when we cannot say.

    None — not an exception and not a guess — for every failure mode: no API key, a stop
    with no coordinates, an HTTP error, a rate limit, or no drivable route. Callers derive
    a drop-off from the vehicle's billed minimum in that case, so the feature going dark
    degrades a trip's end time rather than blocking the save that carries it.
    """
    if not settings.LOCATIONIQ_API_KEY:
        return None
    try:
        coords = [round(float(v), _COORD_PRECISION) for v in (from_lat, from_lon, to_lat, to_lon)]
    except (TypeError, ValueError):
        return None
    a_lat, a_lon, b_lat, b_lon = coords
    # LocationIQ takes lon,lat — the reverse of every other coordinate in this file.
    path = f"{a_lon},{a_lat};{b_lon},{b_lat}"
    key = f"liq-dir:{path}"
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    try:
        resp = requests.get(
            f"{DIRECTIONS_URL}/{path}",
            params={"key": settings.LOCATIONIQ_API_KEY, "overview": "false"},
            timeout=TIMEOUT,
        )
        if resp.status_code >= 400:
            # Not cached: a 429 is transient and caching it would blank every drop-off
            # derived for the next week.
            logger.warning(
                "LocationIQ directions %s for %s: %s",
                resp.status_code,
                path,
                (resp.text or "")[:200],
            )
            return None
        routes = (resp.json() or {}).get("routes") or []
        duration = routes[0].get("duration") if routes else None
        seconds = int(round(float(duration))) if duration is not None else None
    except (requests.RequestException, ValueError, TypeError, AttributeError, IndexError):
        return None
    if seconds is None:
        return None
    cache.set(key, seconds, DIRECTIONS_CACHE_SECONDS)
    return seconds
