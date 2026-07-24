"""Geo helpers safe to import from settings (no Django imports)."""


def parse_latlon(raw: str | None, default: tuple[float, float]) -> tuple[float, float]:
    """Parse a "lat,lon" string to a (lat, lon) float tuple; return default if malformed."""
    try:
        lat, lon = (raw or "").split(",")
        return (float(lat.strip()), float(lon.strip()))
    except (ValueError, AttributeError):
        return default
