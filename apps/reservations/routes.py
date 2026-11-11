"""Cached full-route geometry for the driver page."""

import hashlib
from collections.abc import Iterable

from django.core.cache import cache

from apps.integrations import geocoding
from apps.reservations.models import Reservation, Stop

FAILURE_BACKOFF_SECONDS = 10 * 60


def stop_signature(stops: Iterable[Stop]) -> str:
    """Hash of every stop's coordinates in order; a stop without coordinates is "-"."""
    parts = [
        f"{s.latitude:.6f},{s.longitude:.6f}"
        if s.latitude is not None and s.longitude is not None
        else "-"
        for s in stops
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def route_for(trip: Reservation) -> str:
    """The trip's route polyline, recomputed only when its stops moved.

    Returns "" and writes nothing when a stop lacks coordinates or the lookup fails; a
    failed lookup is not retried for ten minutes unless the stops change.
    """
    stops = list(trip.ordered_stops)
    signature = stop_signature(stops)
    if trip.route_geometry and trip.route_signature == signature:
        return trip.route_geometry
    if len(stops) < 2 or any(s.latitude is None or s.longitude is None for s in stops):
        return ""
    failed_key = f"route-failed:{trip.pk}:{signature}"
    if cache.get(failed_key):
        return ""
    geometry = geocoding.route_polyline([(float(s.latitude), float(s.longitude)) for s in stops])
    if not geometry:
        # Back off so a slow or down provider cannot stall every page load for this trip.
        cache.set(failed_key, True, FAILURE_BACKOFF_SECONDS)
        return ""
    trip.route_geometry = geometry
    trip.route_signature = signature
    trip.save(update_fields=["route_geometry", "route_signature", "updated_at"])
    return geometry
