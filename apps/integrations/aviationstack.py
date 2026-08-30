"""aviationstack client — the HTTP boundary for flight verification (spec 2026-08-29 §5).

Two endpoints, one normalized `FlightResult`; nothing above this module ever sees the JSON
shapes aviationstack uses. `/v1/flightsFuture` (more than 7 days out) sends lowercase
camelCase blocks with a space-separated local `"YYYY-MM-DD HH:MM:SS"` scheduledTime — not
the bare `HH:MM` the published docs describe (task-3R-brief.md §1, ground truth captured
2026-08-29). `/v1/flights` (real-time) is not reachable on this subscription — 403
`function_access_restricted`, confirmed on the live key — so the live path calls
`/v1/timetable` instead. Its timestamps are naive ISO-T and **airport-local**, never UTC;
treating them as UTC would be silently wrong by the airport's offset (task-3R-brief.md §2).
Pure functions that make a request only when called — the cache-first, rate-limit-aware
orchestration lives in apps/reservations/flights.py. Mirrors geocoding.py / gnet.py: module
constants, one private `_request`, typed public functions, one exception carrying enough to
branch on. Always mock `requests` in tests.
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

# /v1/timetable `status` values -> reservations.Flight.Status values (same strings).
# `/v1/flights` is unusable on this plan (403 function_access_restricted) — see live_flight.
_LIVE_STATUSES = {"scheduled", "active", "landed", "cancelled", "diverted", "incident"}

# aviationstack's `redirected` is a synonym for our `diverted` bucket (task-3R-brief.md §2).
# `unknown` is deliberately NOT mapped here — it falls through to the warning + scheduled
# default in live_flight below, same as any other value this client doesn't recognize.
_STATUS_ALIASES = {"redirected": "diverted"}


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
        # Documented shape is {"code": ..., "message": ...}, but a live 429 (browser
        # probe, 2026-08-29) came back as a bare string instead — accept either so a
        # plan/rate-limit hiccup degrades to a mapped AviationstackError, never a 500.
        err = body.get("error") or {}
        if isinstance(err, dict):
            api_code = str(err.get("code") or "")
            message = str(err.get("message") or resp.text or "")
        else:
            api_code = ""
            message = str(err or resp.text or "")
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
    """flightsFuture's real `scheduledTime` (task-3R-brief.md §1, ground truth 2026-08-29):
    a full space-separated local datetime, `"2026-09-28 20:00:00"` — not the bare `"HH:MM"`
    the published docs describe. Both are accepted, along with an ISO-T form some older docs
    use, so a documentation drift in either direction doesn't silently drop the time again."""
    text = _s(value)
    if not text:
        return None
    try:
        if "T" in text or " " in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).time().replace(tzinfo=None)
        return time.fromisoformat(text[:8] if len(text) > 5 else text)
    except ValueError:
        return None


def _local_to_utc(day: date_type, local: time | None, tz_name: str) -> datetime | None:
    if local is None:
        return None
    # fold=0: an ambiguous DST-end time resolves to the first occurrence, never raises.
    return datetime.combine(day, local, tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def _local_iso_to_utc(value, tz_name: str) -> datetime | None:
    """/v1/timetable's `scheduledTime`/`estimatedTime`/`actualTime`: naive ISO-T
    (`"2026-08-29T12:06:00.000"`, no offset) that is **airport-local**, not UTC
    (task-3R-brief.md §2 — the trap that produces silently-wrong times). Splits the parsed
    value back into day + time-of-day and hands it to `_local_to_utc`, the same fold=0
    conversion `future_schedule` already uses, rather than treating the naive value as UTC.
    An aware value (not observed, but tolerated) is trusted and just converted."""
    text = _s(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return _local_to_utc(parsed.date(), parsed.time(), tz_name)


def _iso_local_time(value) -> time | None:
    """Time-of-day exactly as printed in an ISO 8601 string — the offset, if any, is read
    and then dropped, never shifted to UTC. This is the airport-local time-of-day, which is
    what a `preferred_time` argument (always a stop's naive local pickup/scheduled time) has
    to be compared against when `_pick` breaks a tie; comparing it against a UTC-shifted
    clock instead silently favors the wrong flight at any airport that isn't UTC itself. A
    naive value (no offset) is taken at face value too — for a tie-break there's nothing to
    gain by guessing an offset, and the printed digits are the best signal available."""
    text = _s(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).time()
    except ValueError:
        return None


def _iso_local_date(value) -> date_type | None:
    """The calendar date printed in an ISO 8601 string, exactly as printed — same rule as
    `_iso_local_time`, no UTC shift. Used to filter `/v1/timetable` entries down to the date
    `live_flight` was actually asked for (see the docstring there)."""
    text = _s(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _minutes(t: time | None) -> int | None:
    return None if t is None else t.hour * 60 + t.minute


def _delay_minutes(value) -> int | None:
    """timetable's `delay` is a string, not a number, and a live probe already proved this
    API sends junk in fields the docs describe as clean (the string-`error` 429 above is the
    same family of surprise). `int("n/a")` used to raise ValueError, which escaped live_flight
    and lookup as an unhandled 500 with no toast. Junk -> None (unknown); a negative value
    clamps to 0 rather than failing to save against Flight.delay_minutes's
    PositiveIntegerField — early is not a state anyone dispatches around anyway."""
    text = _s(value)
    if not text:
        return None
    try:
        minutes = int(text)
    except ValueError:
        return None
    return max(0, minutes)


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


def _titled(value) -> str:
    """flightsFuture prints display text lowercase — `"air canada"`, gate `"c3"`
    (task-3R-brief.md §4). Title-case it for dispatchers. Only ever applied to
    flightsFuture's own display strings (carrier name, terminal, gate) — never to the codes
    used for matching (already `.upper()`-d separately), and never on the /v1/timetable
    path, which already comes back properly cased (double-applying would mangle a name like
    "ATI Jet" into "Ati Jet")."""
    text = _s(value)
    return text.title() if text else text


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
    """`/v1/flightsFuture` — scheduled flights more than 7 days out. Times arrive as a
    space-separated local `"YYYY-MM-DD HH:MM:SS"` (not the bare `HH:MM` the published docs
    describe — see `_hhmm` and the module docstring); `airport_tz` turns them into UTC."""
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
    cs_airline = (entry.get("codeshared") or {}).get("airline") or {}
    op_iata, op_name = _operated_by(
        _s(cs_airline.get("iataCode")).upper(), _s(cs_airline.get("name")), airline_iata
    )
    return FlightResult(
        found=True,
        status="scheduled",
        scheduled_at=_local_to_utc(date, _hhmm(side.get("scheduledTime")), airport_tz),
        terminal=_titled(side.get("terminal")),
        gate=_titled(side.get("gate")),
        other_airport_iata=_s(other.get("iataCode")).upper(),
        other_airport_name="",  # flightsFuture has no airport-name field either — see
        # apps/reservations/flights.py::lookup, which resolves it from our own Airport table
        operated_by_iata=op_iata,
        operated_by_name=_titled(op_name),
        raw=entry,
    )


def live_flight(
    *,
    airport_iata: str,
    direction: str,
    date: date_type,
    airline_iata: str,
    flight_number: str,
    airport_tz: str,
    preferred_time: time | None = None,
) -> FlightResult:
    """`/v1/timetable` — day-of status, the only live endpoint reachable on this plan
    (`/v1/flights` 403s — see the module docstring). Filtered server-side by `iataCode` +
    `type` + `flight_iata` only — the API takes no `date` param here, so `date` is enforced
    client-side against each entry's own local scheduled date. That matters because
    `LIVE_LOOKAHEAD_DAYS = 0` is the only thing keeping this endpoint same-day today; if that
    constant is ever raised (a plan upgrade), a stale entry from an adjacent day must not be
    silently accepted. Times arrive as naive ISO-T airport-local timestamps —
    `_local_iso_to_utc` converts them with `airport_tz`, the same fold=0 rule
    `future_schedule` uses for flightsFuture, so a DST-ambiguous moment resolves the same
    way on both paths."""
    body = _request(
        "timetable",
        {
            "iataCode": airport_iata.upper(),
            "type": direction,
            "flight_iata": f"{airline_iata.upper()}{flight_number}",
        },
    )
    here, there = _sides(direction)
    ours = [
        e
        for e in _entries(body)
        if _s((e.get(here) or {}).get("iataCode")).upper() == airport_iata.upper()
        and _iso_local_date((e.get(here) or {}).get("scheduledTime")) == date
    ]

    def local_time(entry: dict) -> time | None:
        return _iso_local_time((entry.get(here) or {}).get("scheduledTime"))

    entry = _pick(ours, preferred_time, local_time)
    if entry is None:
        return NOT_FOUND
    side = entry.get(here) or {}
    other = entry.get(there) or {}
    status = _s(entry.get("status")).lower()
    status = _STATUS_ALIASES.get(status, status)
    if status not in _LIVE_STATUSES:
        log.warning("aviationstack: unknown timetable status %r — treated as scheduled", status)
        status = "scheduled"
    cs_airline = (entry.get("codeshared") or {}).get("airline") or {}
    op_iata, op_name = _operated_by(
        _s(cs_airline.get("iataCode")).upper(), _s(cs_airline.get("name")), airline_iata
    )
    delay = side.get("delay")
    return FlightResult(
        found=True,
        status=status,
        scheduled_at=_local_iso_to_utc(side.get("scheduledTime"), airport_tz),
        estimated_at=_local_iso_to_utc(side.get("estimatedTime"), airport_tz),
        actual_at=_local_iso_to_utc(side.get("actualTime"), airport_tz),
        delay_minutes=_delay_minutes(delay),
        terminal=_s(side.get("terminal")),
        gate=_s(side.get("gate")),
        other_airport_iata=_s(other.get("iataCode")).upper(),
        other_airport_name="",  # timetable has no airport-name field
        operated_by_iata=op_iata,
        operated_by_name=op_name,
        raw=entry,
    )
