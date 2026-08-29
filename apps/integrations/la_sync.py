"""Quote -> LimoAnywhere booking sync orchestration.

Payload builders (this section) are pure given geocoded stops; push/provision
functions (later tasks) drive the LA client and log ZapEvents.
Reference shapes: docs/la-api/la-api.txt.
"""

import json
import logging
import secrets
from datetime import datetime
from datetime import time as dtime

from django.conf import settings
from django.core import signing
from django.utils import timezone

from apps.notifications.models import Notification
from apps.reservations.models import Reservation

from . import crypto, geocoding, limoanywhere
from .models import LACustomer, ZapEvent

logger = logging.getLogger(__name__)

IDEMPOTENCY_PREFIX = "create_reservation-res"
WEBHOOK_SALT = "la-webhook"


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
        payload["company"] = contact.company.name
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


def _flight_info(stop) -> dict:
    """LA's `*_flight_info` object — only the halves we have; `{}` means "don't send"."""
    info = {}
    if stop.airline_id:
        info["airline_code"] = stop.airline.iata
    if stop.flight_number:
        info["flight_number"] = stop.flight_number
    return info


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


def build_booking_payload(reservation: Reservation, search_result_id: int | None) -> dict:
    contact = reservation.lead.contact
    first, last = _split_name(contact.name)
    passenger: dict = {"first_name": first, "last_name": last}
    if contact.phone:
        passenger["cellular_phone"] = contact.phone
    if contact.email:
        passenger["email"] = contact.email

    stops = list(reservation.stops.select_related("airline"))
    mid_stops = stops[1:-1]
    note_lines = [
        f"APC quote #{reservation.lead_id} · {reservation.get_trip_type_display()}",
        f"Service: {reservation.service}" if reservation.service else "",
        *(f"Stop: {s.address}" for s in mid_stops),
        f"Quoted line total: ${reservation.line_total}",
    ]
    payload = {
        "search_result_id": search_result_id,
        "passengers": [passenger],
        "payment_type_id": settings.LA_PAYMENT_TYPE_ID,
        "notes": "\n".join(line for line in note_lines if line),
    }
    if stops and _flight_info(stops[0]):
        payload["pickup_flight_info"] = _flight_info(stops[0])
    if reservation.trip_type == Reservation.TripType.TRANSFER and len(stops) >= 2:
        if _flight_info(stops[-1]):
            payload["dropoff_flight_info"] = _flight_info(stops[-1])
    return payload


def webhook_uri(la_customer: LACustomer) -> str:
    """Public inbound-webhook URL for this customer ('' when no public base is set)."""
    base = (settings.LA_WEBHOOK_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    token = signing.dumps(la_customer.pk, salt=WEBHOOK_SALT)
    return f"{base}/webhooks/limoanywhere/{token}/"


def ensure_la_customer(contact) -> LACustomer:
    """Register the contact in LA once (generated password, encrypted at rest)."""
    existing = LACustomer.objects.filter(contact=contact).first()
    if existing is not None:
        return existing
    email = (contact.email or "").strip()
    if not email:
        raise LASyncError("Contact has no email — add one before LimoAnywhere sync.")
    password = secrets.token_urlsafe(16)
    result = limoanywhere.register_customer(build_registration_payload(contact, password=password))
    la_customer = LACustomer.objects.create(
        contact=contact,
        la_customer_id=str(result["id"]),
        la_account_number=str(result.get("number") or ""),
        email_used=email,
        password_encrypted=crypto.encrypt(password),
    )
    contact.la_account_id = str(result.get("number") or result["id"])
    contact.save(update_fields=["la_account_id", "updated_at"])
    uri = webhook_uri(la_customer)
    if uri:
        try:
            limoanywhere.subscribe_webhook(uri, token=la_customer.token())
        except limoanywhere.LAAPIError:
            logger.warning("LA webhook subscribe failed for customer %s", la_customer.pk)
    return la_customer


def _build_preview_payloads(reservation) -> dict:
    contact = reservation.lead.contact
    registration = build_registration_payload(contact, password="(generated at send time)")
    try:
        rate_payload = build_rate_lookup_payload(reservation)
    except geocoding.GeocodeError as exc:
        rate_payload = {"geocode_error": str(exc)}
    except LASyncError as exc:
        rate_payload = {"error": str(exc)}
    return {
        "registration": registration,
        "rate_lookup": rate_payload,
        "booking": build_booking_payload(reservation, None),
    }


def _fail(event: ZapEvent, reservation, message: str) -> ZapEvent:
    event.result = ZapEvent.Result.ERROR
    event.response = message
    event.save(update_fields=["result", "response", "updated_at"])
    Notification.notify(
        reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title="LimoAnywhere sync failed",
        detail=f"Trip #{reservation.pk}: {message}"[:255],
    )
    return event


def _is_preview() -> bool:
    """True unless LA is both armed (`LA_ACTIVE`) and credentialed.

    Credentials alone are not consent to book: `LA_BASE_URL` defaults to production, so a
    send creates a real reservation in the client's LimoAnywhere account. `LA_ACTIVE`
    defaults False and is armed deliberately when Phase 1 lands.
    """
    return not settings.LA_ACTIVE or not limoanywhere.is_configured()


def push_reservation(reservation) -> ZapEvent:
    """Create this trip in LA (or record a preview). Idempotent per reservation."""
    event, _ = ZapEvent.objects.get_or_create(
        lead=reservation.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"{IDEMPOTENCY_PREFIX}{reservation.pk}",
    )
    if event.result == ZapEvent.Result.SUCCESS:
        return event

    if _is_preview():
        # Logged, not silent: in production this is the difference between "Phase 1 hasn't
        # landed yet" and "bookings are vanishing". The PREVIEW event holds the exact payload
        # that would have gone out, so nothing is lost — it is replayed by retry_failed_pushes
        # once LA_ACTIVE is armed.
        logger.warning(
            "LimoAnywhere send skipped for reservation %s — LA_ACTIVE=%s, credentials=%s. "
            "Recorded as a PREVIEW ZapEvent; nothing was sent.",
            reservation.pk,
            settings.LA_ACTIVE,
            limoanywhere.is_configured(),
        )
        event.payload = _build_preview_payloads(reservation)
        event.result = ZapEvent.Result.PREVIEW
        event.response = "Preview — nothing sent to LimoAnywhere."
        event.save(update_fields=["payload", "result", "response", "updated_at"])
        return event

    try:
        la_customer = ensure_la_customer(reservation.lead.contact)
        token = la_customer.token()
        rate_payload = build_rate_lookup_payload(reservation)
        rate = limoanywhere.rate_lookup(rate_payload, token=token)
        results = rate.get("results") or []
        if not results:
            raise LASyncError(
                "rate_lookup returned no results — add a $0 rate per vehicle type in LA."
            )
        booking_payload = build_booking_payload(reservation, results[0]["id"])
        booked = limoanywhere.create_booking(booking_payload, token=token)
    except (LASyncError, geocoding.GeocodeError, limoanywhere.LAAPIError) as exc:
        return _fail(event, reservation, str(exc))

    reservation.la_reservation_id = str(booked.get("id") or "")
    reservation.la_confirmation = str(booked.get("confirmation_number") or "")
    reservation.save(update_fields=["la_reservation_id", "la_confirmation", "updated_at"])
    event.payload = {"rate_lookup": rate_payload, "booking": booking_payload}
    event.result = ZapEvent.Result.SUCCESS
    event.response = json.dumps(booked)
    event.save(update_fields=["payload", "result", "response", "updated_at"])
    return event


def push_lead_bookings(lead) -> list[ZapEvent]:
    """Push every trip on the lead. Best-effort: one failure never blocks the rest."""
    events = []
    for reservation in lead.reservations.all():
        try:
            events.append(push_reservation(reservation))
        except Exception:
            logger.exception("Unexpected LA push failure for reservation %s", reservation.pk)
    return events


LA_EVENT_TO_TRIP_STATUS: dict[str, str] = {
    "reservation.created": Reservation.TripStatus.PENDING,
    "reservation.booked": Reservation.TripStatus.UNASSIGNED,
    "reservation.accepted": Reservation.TripStatus.UNASSIGNED,
    "reservation.driver_was_assigned": Reservation.TripStatus.ASSIGNED,
    "reservation.driver_was_unassigned": Reservation.TripStatus.UNASSIGNED,
    "reservation.driver_departed_to_pickup": Reservation.TripStatus.ON_THE_WAY,
    "reservation.driver_arrived_at_pickup_and_waiting": Reservation.TripStatus.ARRIVED,
    "reservation.driver_arrived_at_pickup_and_circling": Reservation.TripStatus.CIRCLING,
    "reservation.departed_from_pickup": Reservation.TripStatus.CUSTOMER_IN_CAR,
    "reservation.arrived_at_dropoff": Reservation.TripStatus.DONE,
    "reservation.completed": Reservation.TripStatus.DONE,
    "reservation.closed": Reservation.TripStatus.DONE,
    "reservation.cancelled": Reservation.TripStatus.CANCELLED,
}


def retry_failed_pushes() -> int:
    """Cron job: re-run every ERROR push (plus PREVIEW ones once LA is configured).

    PREVIEW events are only stale, not failed — retrying them while LA is still previewing
    would just regenerate the same preview payload, so they're only picked up once LA is
    armed AND credentialed (the credential-day switchover).
    """
    results = [ZapEvent.Result.ERROR]
    if not _is_preview():
        results.append(ZapEvent.Result.PREVIEW)

    count = 0
    candidates = ZapEvent.objects.filter(
        action=ZapEvent.Action.CREATE_RESERVATION, result__in=results
    )
    for event in candidates:
        pk_text = event.idempotency_key.removeprefix(IDEMPOTENCY_PREFIX)
        if not pk_text.isdigit():
            continue
        reservation = Reservation.objects.filter(pk=int(pk_text)).first()
        if reservation is None:
            continue
        if push_reservation(reservation).result == ZapEvent.Result.SUCCESS:
            count += 1
    return count
