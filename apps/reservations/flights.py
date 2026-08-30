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

from .models import LIVE_PHASE_DAYS, Flight, Stop, today_at

log = logging.getLogger(__name__)

# How many days ahead the live path answers for. Settled by Moe's probe on the real key
# (2026-08-29, task-3R-brief.md): /v1/flights is unusable on this plan (403
# function_access_restricted) and /v1/flightsFuture hard-refuses inside 7 days (500 "date
# must be above <today+7>"), so /v1/timetable — day-of only — is the entire live surface.
# Days 1-7 fall through to UNAVAILABLE with zero calls; its copy ("Live data available on
# the day of travel") is now literally true rather than aspirational.
LIVE_LOOKAHEAD_DAYS = 0

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


def _other_airport_name(iata: str) -> str:
    """Resolve the far-end airport's name from our own 863-row `addresses.Airport` table.

    Neither aviationstack endpoint ever sends an airport-name field (final review #1) — one
    indexed query, only reached when a call actually returned an IATA code, never per render.
    Blank when the code isn't in our table (a foreign airport, or no code at all)."""
    if not iata:
        return ""
    return Airport.objects.filter(iata=iata).values_list("name", flat=True).first() or ""


def _keep(new: str, old: str) -> str:
    """A blank on a refresh means "not reported," never "erase" (final review #2) — timetable
    frequently sends null terminal/gate and no airport-name field at all, and overwriting a
    richer flightsFuture-sourced row with those blanks would drop detail a dispatcher already
    had. `old` is only ever the previously cached value, so there's nothing to keep on a
    first-ever lookup."""
    return new or old


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


def link_stop(stop_id: int, flight: Flight) -> None:
    """Link one saved stop to the flight just verified — the drawer's own save path, since
    it has none through the editor (dispatch gap fix, 2026-08-29). Only called when the
    request carried a `stop` id; the editor's payload never does.

    Never raises and never fails the request it's called from: the lookup already
    succeeded and its pill is valid regardless of this write. A stop that no longer
    matches what was verified — most likely a stale drawer (the editor saves stops by
    delete-and-recreate, so ids churn on every save), possibly a forged id — is logged and
    skipped rather than linking an arbitrary stop to an arbitrary flight.
    """
    stop = Stop.objects.select_related("reservation").filter(pk=stop_id).first()
    if stop is None:
        log.warning("flight_verify: stop %s not found — not linking", stop_id)
        return
    if (
        stop.airport_id != flight.airport_id
        or stop.airline_id != flight.airline_id
        or stop.flight_number != flight.flight_number
        or stop.flight_direction != flight.direction
        or stop.reservation.pickup_date != flight.flight_date
    ):
        log.warning(
            "flight_verify: stop %s no longer matches flight %s — not linking (stale drawer?)",
            stop_id,
            flight.pk,
        )
        return
    stop.flight = flight
    stop.save(update_fields=["flight"])


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
    existing = Flight.objects.select_related("airline", "airport").filter(**key).first()
    if existing is not None and now < existing.refresh_allowed_at:
        return existing  # the window decides; there is no force flag

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
        result = aviationstack.live_flight(airport_tz=airport.timezone, **common)
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
    other_airport_name = result.other_airport_name or _other_airport_name(result.other_airport_iata)
    row, _ = Flight.objects.update_or_create(
        **key,
        defaults={
            "status": status,
            "scheduled_at": result.scheduled_at,
            "estimated_at": result.estimated_at,
            "actual_at": result.actual_at,
            "delay_minutes": result.delay_minutes,
            "terminal": _keep(result.terminal, existing.terminal if existing else ""),
            "gate": _keep(result.gate, existing.gate if existing else ""),
            "other_airport_iata": _keep(
                result.other_airport_iata, existing.other_airport_iata if existing else ""
            ),
            "other_airport_name": _keep(
                other_airport_name, existing.other_airport_name if existing else ""
            ),
            "operated_by_iata": result.operated_by_iata,
            "operated_by_name": result.operated_by_name,
            "source": source,
            "checked_at": now,
            "raw": result.raw,
        },
    )
    return row
