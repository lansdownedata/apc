"""Directory matching for the public autocompletes.

Airport results are shaped exactly like `geocoding._decompose()` output so the two
geocode proxies can prepend them to the LocationIQ list without any consumer knowing.
Venue results have their own shape — they feed the wedding intake, not an address field.
"""

from django.db.models import Case, IntegerField, Q, Value, When

from .models import Airport, Venue

MIN_QUERY = 2
# Name/city matches against medium_airport need at least this many characters. 768 of the
# 863 rows are medium — without the gate, regional airstrips bury ordinary address lookups.
MEDIUM_NAME_MIN = 4
AIRPORT_RESULT_LIMIT = 3
# A "word" starts at the beginning of the string or immediately after one of these.
WORD_SEPARATORS = (" ", "-", "/")


def _word_prefix_q(field: str, q: str) -> Q:
    condition = Q(**{f"{field}__istartswith": q})
    for separator in WORD_SEPARATORS:
        condition |= Q(**{f"{field}__icontains": f"{separator}{q}"})
    return condition


def _serialize(airport: Airport) -> dict:
    return {
        "landmark_name": airport.name,
        "line1": airport.line1,
        "line2": "",
        "city": airport.city,
        "state": airport.state,
        "postal": airport.postal,
        "country": "US",
        "latitude": str(airport.latitude),
        "longitude": str(airport.longitude),
        "place_id": airport.locationiq_place_id,
        "place_type": "aerodrome",
        "place_class": "aeroway",
        "display_name": (
            airport.display_name or f"{airport.name}, {airport.city}, {airport.state}"
        ),
        "is_airport": True,
        "airport_code": airport.iata,
        "airport_id": airport.pk,
        # Carried through to the editor's draft so its Verify button can gate on it
        # without a round trip (spec 2026-08-29 finding 2).
        "has_scheduled_service": airport.has_scheduled_service,
    }


def search_airports(q: str, limit: int = AIRPORT_RESULT_LIMIT) -> list[dict]:
    """Airports matching `q`, best first, ready to prepend to LocationIQ results."""
    q = (q or "").strip()
    if len(q) < MIN_QUERY:
        return []

    code_match = Q(iata__iexact=q) | Q(icao__iexact=q) | Q(ident__iexact=q)
    name_match = _word_prefix_q("name", q) | _word_prefix_q("city", q)
    if len(q) < MEDIUM_NAME_MIN:
        name_match &= Q(size=Airport.Size.LARGE)

    rows = (
        Airport.objects.filter(is_active=True, serves_ground_transport=True)
        .filter(code_match | name_match)
        .annotate(
            code_rank=Case(
                When(code_match, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            size_rank=Case(
                When(size=Airport.Size.LARGE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
        )
        .order_by("code_rank", "size_rank", "name")[:limit]
    )
    return [_serialize(a) for a in rows]


VENUE_RESULT_LIMIT = 8
MIN_VENUE_QUERY = 2
# When the curated directory returns fewer than this many *name* matches for a query,
# the public endpoint also fetches LocationIQ and merges the two lists — a single weak
# (city-only) directory hit must not hide a real venue we have never run to (§A1).
VENUE_STRONG_MATCH_TARGET = 3


def _serialize_venue(venue: Venue) -> dict:
    return {
        "id": venue.pk,
        "name": venue.name,
        "kind": venue.kind,
        "address": venue.address,
        "city": venue.city,
        "state": venue.state,
        "location_line": venue.location_line,
        "vehicle_cap": venue.vehicle_cap,
        "cap_note": venue.cap_note,
        "lead_hits": venue.lead_hits,
        "latitude": str(venue.latitude) if venue.latitude is not None else None,
        "longitude": str(venue.longitude) if venue.longitude is not None else None,
        "source": "directory",
    }


def search_venues(q: str, kind: str = "", limit: int = VENUE_RESULT_LIMIT) -> list[dict]:
    """Venues/hotels/ceremony sites matching `q`, most-quoted first.

    `-lead_hits` before name so the places the office actually runs to every weekend
    surface first — a couple searching "hampton" wants the Leesburg one we've quoted 33
    times, not alphabetical order. An unrecognised `kind` is ignored rather than
    rejected: a stale value in a query string should narrow nothing, not 400.
    """
    q = (q or "").strip()
    if len(q) < MIN_VENUE_QUERY:
        return []
    rows = Venue.objects.filter(is_active=True).filter(Q(name__icontains=q) | Q(city__icontains=q))
    if kind in Venue.Kind.values:
        rows = rows.filter(kind=kind)
    return [_serialize_venue(v) for v in rows.order_by("-lead_hits", "name")[:limit]]
