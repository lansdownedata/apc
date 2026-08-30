"""Airport matching for the address autocompletes.

Results are shaped exactly like `geocoding._decompose()` output so the two geocode
proxies can prepend them to the LocationIQ list without any consumer knowing.
"""

from django.db.models import Case, IntegerField, Q, Value, When

from .models import Airport

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
