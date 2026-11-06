"""Pickup-stop → IANA timezone. Offline, never raises, cheapest source first."""

from timezonefinder import TimezoneFinder


class _Finder:
    """Thin wrapper so tests can monkeypatch ``timezone_at`` on the instance."""

    def __init__(self) -> None:
        self._inner = TimezoneFinder()

    def timezone_at(self, **kwargs):
        return self._inner.timezone_at(**kwargs)


_finder = _Finder()  # construction loads polygons; do it once per process


def resolve(stop) -> str:
    """IANA zone for a pickup stop. Cheapest source first; never raises.

    1. `stop.airport.timezone` when present (already exact on every airport row).
    2. `timezonefinder` on `stop.latitude` / `stop.longitude` (`lng=`/`lat=`).
    3. `""` when there is no airport zone and no usable coordinate (or the
       finder returns None — open water / no polygon).
    """
    airport = getattr(stop, "airport", None)
    airport_tz = getattr(airport, "timezone", "") if airport is not None else ""
    if airport_tz:
        return airport_tz
    lat = getattr(stop, "latitude", None)
    lng = getattr(stop, "longitude", None)
    if lat is None or lng is None:
        return ""
    try:
        found = _finder.timezone_at(lng=float(lng), lat=float(lat))
    except Exception:
        return ""
    return found or ""


def timezone_at(lat, lon) -> str | None:
    """IANA zone for a coordinate pair, or None when missing, invalid or unresolvable."""
    if lat is None or lon is None:
        return None
    try:
        return _finder.timezone_at(lng=float(lon), lat=float(lat)) or None
    except Exception:
        return None
