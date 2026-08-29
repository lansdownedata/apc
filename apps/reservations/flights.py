"""Flight verification service (spec 2026-08-29 §6).

`lookup` is the only path to the aviationstack cache: it validates, serves a cached row
while its re-check window is open, otherwise routes to the endpoint the date allows and
upserts the answer. Nothing is cached when the provider errors. `link_flights` derives a
stop's link to that cache on every save.
"""

from __future__ import annotations

import logging
import re
from datetime import date, time

from django.utils import timezone

from apps.addresses.models import Airline, Airport
from apps.integrations import aviationstack

from .models import LIVE_PHASE_DAYS, Flight, today_at

log = logging.getLogger(__name__)

# How many days ahead /v1/flights answers for. The docs do not say; Moe's probe (Task 1)
# does: keep 7 if `--date <today+3d>` returned rows, set 0 if it came back empty (then days
# 1–7 read "Live on the day" until the day itself).
LIVE_LOOKAHEAD_DAYS = 7

# 3-char codes only. Small fields carry local idents like "07FA" in `Airport.iata`; the API
# cannot look those up, and the factory's "T00"-style codes must still pass in tests.
_IATA_RE = re.compile(r"^[A-Z0-9]{3}$")

_KEY_FIELDS = ("airport_id", "airline_id", "flight_number", "flight_direction")


class FlightLookupError(ValueError):
    """Refused before any call: `code` ∈ no_iata · no_timezone · past_date."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def link_flights(stops: list[dict], pickup_date: date | None) -> None:
    """Set `flight_id` on each parsed stop dict from the cache — one query per save.

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


def lookup(
    *,
    airline: Airline,
    flight_number: str,
    airport: Airport,
    direction: str,
    flight_date: date,
    preferred_time: time | None = None,
) -> Flight:
    """Return the cached row for this flight-at-airport, refreshing it from aviationstack
    only when its re-check window has passed (spec §6.1). Raises FlightLookupError for
    input we refuse to send, AviationstackError when the provider fails (nothing cached)."""
    iata = (airport.iata or "").upper()
    if not _IATA_RE.match(iata):
        raise FlightLookupError("no_iata", "This airport has no IATA code to look up.")
    if not airport.timezone:
        log.warning("Airport %s has no timezone — flight lookup refused", iata)
        raise FlightLookupError("no_timezone", f"{iata} has no time zone on file.")
    today = today_at(airport.timezone)
    if flight_date < today:
        raise FlightLookupError("past_date", "The trip date has passed.")

    key = {
        "airline": airline,
        "flight_number": flight_number,
        "flight_date": flight_date,
        "airport": airport,
        "direction": direction,
    }
    now = timezone.now()
    row = Flight.objects.select_related("airline", "airport").filter(**key).first()
    if row is not None and now < row.refresh_allowed_at:
        return row  # the window decides; there is no force flag

    days_out = (flight_date - today).days
    common = {
        "airport_iata": iata,
        "direction": direction,
        "date": flight_date,
        "airline_iata": airline.iata,
        "flight_number": flight_number,
        "preferred_time": preferred_time,
    }
    if days_out > LIVE_PHASE_DAYS:
        result = aviationstack.future_schedule(airport_tz=airport.timezone, **common)
        source = Flight.Source.FUTURE
    elif days_out <= LIVE_LOOKAHEAD_DAYS:
        result = aviationstack.live_flight(**common)
        source = Flight.Source.LIVE
    else:
        result = aviationstack.FlightResult(found=False, status=Flight.Status.UNAVAILABLE)
        source = ""

    if result.found:
        status = result.status
    elif result.status == Flight.Status.UNAVAILABLE:
        status = Flight.Status.UNAVAILABLE
    else:
        status = Flight.Status.NOT_FOUND
    row, _ = Flight.objects.update_or_create(
        **key,
        defaults={
            "status": status,
            "scheduled_at": result.scheduled_at,
            "estimated_at": result.estimated_at,
            "actual_at": result.actual_at,
            "delay_minutes": result.delay_minutes,
            "terminal": result.terminal,
            "gate": result.gate,
            "other_airport_iata": result.other_airport_iata,
            "other_airport_name": result.other_airport_name,
            "operated_by_iata": result.operated_by_iata,
            "operated_by_name": result.operated_by_name,
            "source": source,
            "checked_at": now,
            "raw": result.raw,
        },
    )
    return row
