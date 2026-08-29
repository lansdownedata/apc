"""Flight verification service (spec 2026-08-29 §6): the derived stop→flight link now,
`lookup` (cache-first, phase-routed, window-enforced) in the next task."""

from __future__ import annotations

from datetime import date

from .models import Flight, today_at  # noqa: F401 — today_at re-exported for callers

_KEY_FIELDS = ("airport_id", "airline_id", "flight_number", "flight_direction")


def link_flights(stops: list[dict], pickup_date: date | None) -> None:
    """Set `flight_id` on each parsed stop dict from the cache — one query no matter how
    many stops.

    A stop links when a cached row exists for its (airline, number, trip date, airport,
    direction). Editing any of those drops the link, so a verified stop stays verified
    across unrelated edits and reads as unverified the moment its flight changes.
    """
    for stop in stops:
        stop["flight_id"] = None
    keyed = [s for s in stops if all(s.get(f) for f in _KEY_FIELDS)]
    if pickup_date is None or not keyed:
        return
    rows = Flight.objects.filter(
        flight_date=pickup_date, airport_id__in={s["airport_id"] for s in keyed}
    ).values_list("pk", "airline_id", "flight_number", "airport_id", "direction")
    index = {(air, num, apt, direction): pk for pk, air, num, apt, direction in rows}
    for stop in keyed:
        key = (
            stop["airline_id"],
            stop["flight_number"],
            stop["airport_id"],
            stop["flight_direction"],
        )
        stop["flight_id"] = index.get(key)
