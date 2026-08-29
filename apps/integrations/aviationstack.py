"""aviationstack client — the HTTP boundary for flight verification (spec 2026-08-29 §5).

Two endpoints, one normalized `FlightResult`; nothing above this module ever sees the JSON
shapes aviationstack uses (camelCase local `HH:MM` on /v1/flightsFuture, snake_case ISO on
/v1/flights). Pure functions that make a request only when called — the cache-first,
rate-limit-aware orchestration lives in apps/reservations/flights.py. Mirrors
geocoding.py / gnet.py: module constants, one private `_request`, typed public functions,
one exception carrying enough to branch on. Always mock `requests` in tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from datetime import date as date_type
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

log = logging.getLogger(__name__)

TIMEOUT = 15
ARRIVAL = "arrival"
DEPARTURE = "departure"

# /v1/flights `flight_status` values -> reservations.Flight.Status values (same strings).
_LIVE_STATUSES = {"scheduled", "active", "landed", "cancelled", "diverted", "incident"}


class AviationstackError(Exception):
    """A call that produced no usable result. `code` is the branch key for callers:
    not_configured · invalid_key · plan · not_found_endpoint · rate_limited · quota ·
    server · transport · bad_response. Nothing is ever cached on one of these."""

    def __init__(self, code: str, message: str, status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"aviationstack {code} ({status}): {message[:300]}")


@dataclass(frozen=True)
class FlightResult:
    """One flight as seen from one airport. All datetimes are aware UTC."""

    found: bool
    status: str = "scheduled"
    scheduled_at: datetime | None = None
    estimated_at: datetime | None = None
    actual_at: datetime | None = None
    delay_minutes: int | None = None
    terminal: str = ""
    gate: str = ""
    other_airport_iata: str = ""
    other_airport_name: str = ""
    operated_by_iata: str = ""
    operated_by_name: str = ""
    raw: dict = field(default_factory=dict)


NOT_FOUND = FlightResult(found=False)


def is_configured() -> bool:
    return bool(settings.AVIATIONSTACK_API_KEY)


# --- plumbing ---


def _map_error(status: int, api_code: str) -> str:
    if status == 401:
        return "invalid_key"
    if status == 403:
        return "plan"
    if status == 404:
        return "not_found_endpoint"
    if status == 429:
        return "quota" if api_code == "usage_limit_reached" else "rate_limited"
    if status >= 500:
        return "server"
    return "bad_response"


def _request(path: str, params: dict) -> dict:
    key = settings.AVIATIONSTACK_API_KEY
    if not key:
        raise AviationstackError("not_configured", "AVIATIONSTACK_API_KEY is not set.")
    url = f"{settings.AVIATIONSTACK_BASE_URL}/v1/{path}"
    try:
        resp = requests.get(url, params={**params, "access_key": key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AviationstackError("transport", str(exc)) from exc
    try:
        body = resp.json() if resp.content else {}
    except ValueError as exc:
        raise AviationstackError("bad_response", resp.text or "", resp.status_code) from exc
    if not isinstance(body, dict):
        raise AviationstackError("bad_response", "unexpected body", resp.status_code)
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        api_code = str(err.get("code") or "")
        message = str(err.get("message") or resp.text or "")
        raise AviationstackError(_map_error(resp.status_code, api_code), message, resp.status_code)
    return body


def _entries(body: dict) -> list[dict]:
    """`data` is documented as [[...]] on flightsFuture and [...] on flights; accept both."""
    out: list[dict] = []
    for item in body.get("data") or []:
        if isinstance(item, list):
            out.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            out.append(item)
    return out


def _s(value) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _hhmm(value) -> time | None:
    """`"17:35"` (documented) or an ISO string (older docs) -> local time-of-day."""
    text = _s(value)
    if not text:
        return None
    try:
        if "T" in text:
            log.warning("aviationstack: ISO scheduledTime on flightsFuture (%s) — verify", text)
            return datetime.fromisoformat(text.replace("Z", "+00:00")).time().replace(tzinfo=None)
        return time.fromisoformat(text[:8] if len(text) > 5 else text)
    except ValueError:
        return None


def _local_to_utc(day: date_type, local: time | None, tz_name: str) -> datetime | None:
    if local is None:
        return None
    # fold=0: an ambiguous DST-end time resolves to the first occurrence, never raises.
    return datetime.combine(day, local, tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def _iso_utc(value) -> datetime | None:
    text = _s(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        log.warning("aviationstack: naive timestamp %s treated as UTC", text)
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_local_time(value) -> time | None:
    """Time-of-day exactly as printed in an ISO 8601 string — the offset, if any, is read
    and then dropped, never shifted to UTC like `_iso_utc` does. This is the airport-local
    time-of-day, which is what a `preferred_time` argument (always a stop's naive local
    pickup/scheduled time) has to be compared against when `_pick` breaks a tie; comparing
    it against `_iso_utc`'s UTC-shifted clock instead silently favors the wrong flight at
    any airport that isn't UTC itself. A naive value (no offset) is taken at face value too
    — for a tie-break there's nothing to gain by guessing an offset, and the printed digits
    are the best signal available."""
    text = _s(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).time()
    except ValueError:
        return None


def _minutes(t: time | None) -> int | None:
    return None if t is None else t.hour * 60 + t.minute


def _pick(entries: list[dict], preferred_time: time | None, local_time) -> dict | None:
    """Direct (non-codeshare) entries first; among those, the one whose `local_time(entry)`
    is closest to `preferred_time`. A codeshare duplicate or the same number flown twice a
    day both land here."""
    if not entries:
        return None
    direct = [e for e in entries if not e.get("codeshared")] or entries
    target = _minutes(preferred_time)
    if target is None or len(direct) == 1:
        return direct[0]

    def distance(entry: dict) -> int:
        minutes = _minutes(local_time(entry))
        return 10**6 if minutes is None else abs(minutes - target)

    return min(direct, key=distance)


def _sides(direction: str) -> tuple[str, str]:
    """(the block at our airport, the block at the other end)."""
    return (ARRIVAL, DEPARTURE) if direction == ARRIVAL else (DEPARTURE, ARRIVAL)


def _operated_by(cs_iata: str, cs_name: str, airline_iata: str) -> tuple[str, str]:
    """A codeshared block names the operating carrier — unless it is the carrier we asked
    for, in which case the entry is a partner's code on that carrier's own metal."""
    if cs_iata and cs_iata != airline_iata.upper():
        return cs_iata, cs_name
    return "", ""


# --- endpoints ---


def future_schedule(
    *,
    airport_iata: str,
    direction: str,
    date: date_type,
    airline_iata: str,
    flight_number: str,
    airport_tz: str,
    preferred_time: time | None = None,
) -> FlightResult:
    """`/v1/flightsFuture` — scheduled flights more than 7 days out. Times arrive as bare
    airport-local `HH:MM`; `airport_tz` turns them into UTC."""
    body = _request(
        "flightsFuture",
        {
            "iataCode": airport_iata.upper(),
            "type": direction,
            "date": date.isoformat(),
            "airline_iata": airline_iata.upper(),
            "flight_number": flight_number,
        },
    )
    here, there = _sides(direction)
    entry = _pick(
        _entries(body), preferred_time, lambda e: _hhmm((e.get(here) or {}).get("scheduledTime"))
    )
    if entry is None:
        return NOT_FOUND
    side = entry.get(here) or {}
    other = entry.get(there) or {}
    other_airport = other.get("airport") if isinstance(other.get("airport"), dict) else {}
    cs_airline = (entry.get("codeshared") or {}).get("airline") or {}
    op_iata, op_name = _operated_by(
        _s(cs_airline.get("iataCode")).upper(), _s(cs_airline.get("name")), airline_iata
    )
    return FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=_local_to_utc(date, _hhmm(side.get("scheduledTime")), airport_tz),
        terminal=_s(side.get("terminal")),
        gate=_s(side.get("gate")),
        other_airport_iata=_s(other.get("iataCode")).upper(),
        other_airport_name=_s(other_airport.get("name")),
        operated_by_iata=op_iata,
        operated_by_name=op_name,
        raw=entry,
    )


def live_flight(
    *,
    airport_iata: str,
    direction: str,
    date: date_type,
    airline_iata: str,
    flight_number: str,
    preferred_time: time | None = None,
) -> FlightResult:
    """`/v1/flights` — real-time status. Filtered by `flight_iata` + `flight_date`, then kept
    only when the flight's arrival (or departure) airport is ours."""
    body = _request(
        "flights",
        {
            "flight_iata": f"{airline_iata.upper()}{flight_number}",
            "flight_date": date.isoformat(),
        },
    )
    here, there = _sides(direction)
    ours = [
        e
        for e in _entries(body)
        if _s((e.get(here) or {}).get("iata")).upper() == airport_iata.upper()
    ]

    def local_time(entry: dict) -> time | None:
        return _iso_local_time((entry.get(here) or {}).get("scheduled"))

    entry = _pick(ours, preferred_time, local_time)
    if entry is None:
        return NOT_FOUND
    side = entry.get(here) or {}
    other = entry.get(there) or {}
    status = _s(entry.get("flight_status")).lower()
    if status not in _LIVE_STATUSES:
        log.warning("aviationstack: unknown flight_status %r — treated as scheduled", status)
        status = "scheduled"
    codeshare = (entry.get("flight") or {}).get("codeshared") or {}
    op_iata, op_name = _operated_by(
        _s(codeshare.get("airline_iata")).upper(), _s(codeshare.get("airline_name")), airline_iata
    )
    delay = side.get("delay")
    return FlightResult(
        found=True,
        status=status,
        scheduled_at=_iso_utc(side.get("scheduled")),
        estimated_at=_iso_utc(side.get("estimated")),
        actual_at=_iso_utc(side.get("actual")),
        delay_minutes=int(delay) if _s(delay) else None,
        terminal=_s(side.get("terminal")),
        gate=_s(side.get("gate")),
        other_airport_iata=_s(other.get("iata")).upper(),
        other_airport_name=_s(other.get("airport")),
        operated_by_iata=op_iata,
        operated_by_name=op_name,
        raw=entry,
    )
