"""Quote -> LimoAnywhere booking sync orchestration.

Payload builders (this section) are pure given geocoded stops; push/provision
functions (later tasks) drive the LA client and log ZapEvents.
Reference shapes: docs/la-api/la-api.txt.
"""

import logging
from datetime import datetime
from datetime import time as dtime

from django.conf import settings
from django.utils import timezone

from apps.reservations.models import Reservation

from . import geocoding

logger = logging.getLogger(__name__)


class LASyncError(Exception):
    """A reservation can't be represented as an LA booking (data problem)."""


def _split_name(name: str) -> tuple[str, str]:
    first, _, last = (name or "").strip().partition(" ")
    return first or "Customer", (last.strip() or "-")


def build_registration_payload(contact, *, password: str) -> dict:
    first, last = _split_name(contact.name)
    payload = {
        "first_name": first,
        "last_name": last,
        "email": (contact.email or "").strip(),
        "password": password,
    }
    if contact.company:
        payload["company"] = contact.company
    if contact.phone:
        payload["cellular_phone1"] = contact.phone
    return payload


def _pickup_at(reservation: Reservation) -> str:
    if reservation.pickup_date is None:
        raise LASyncError("Reservation has no pickup date.")
    naive = datetime.combine(reservation.pickup_date, reservation.pickup_time or dtime(0, 0))
    return timezone.make_aware(naive).isoformat()


def _address_payload(stop) -> dict:
    lat, lng = geocoding.geocode_stop(stop)
    return {
        "address": {
            "address_line1": stop.address,
            "latitude": float(lat),
            "longitude": float(lng),
        }
    }


def build_rate_lookup_payload(reservation: Reservation) -> dict:
    stops = list(reservation.stops.all())
    if not stops:
        raise LASyncError("Reservation has no stops.")
    payload = {
        "result_type": "Mixed",
        "passenger_count": reservation.passengers,
        "scheduled_pickup_at": _pickup_at(reservation),
        "pickup": _address_payload(stops[0]),
    }
    if reservation.trip_type == Reservation.TripType.HOURLY:
        payload["scheduled_duration_in_minutes"] = int(reservation.billed_hours * 60)
    else:
        if len(stops) < 2:
            raise LASyncError("Transfer needs a pickup and a dropoff stop.")
        payload["dropoff"] = _address_payload(stops[-1])
    return payload


def build_booking_payload(
    reservation: Reservation, search_result_id: int | None
) -> dict:
    contact = reservation.lead.contact
    first, last = _split_name(contact.name)
    passenger: dict = {"first_name": first, "last_name": last}
    if contact.phone:
        passenger["cellular_phone"] = contact.phone
    if contact.email:
        passenger["email"] = contact.email

    mid_stops = list(reservation.stops.all())[1:-1]
    note_lines = [
        f"APC quote #{reservation.lead_id} · {reservation.get_trip_type_display()}",
        f"Service: {reservation.service}" if reservation.service else "",
        *(f"Stop: {s.address}" for s in mid_stops),
        f"Quoted line total: ${reservation.line_total}",
    ]
    return {
        "search_result_id": search_result_id,
        "passengers": [passenger],
        "payment_type_id": settings.LA_PAYMENT_TYPE_ID,
        "notes": "\n".join(line for line in note_lines if line),
    }
