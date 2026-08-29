"""Reservation editor endpoints — JSON-draft save, duplicate, delete, flight verify."""

from __future__ import annotations

import json
import logging
from datetime import date, time

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.addresses.models import Airline, Airport
from apps.dispatch import services as dispatch_services
from apps.integrations.aviationstack import AviationstackError
from apps.leads.models import Lead
from apps.notifications.models import Notification

from . import flights
from .drafts import _FLIGHT_RE, DraftError, save_reservation_from_draft
from .flights import FlightLookupError
from .models import FlightDirection, Reservation, Stop

log = logging.getLogger(__name__)

# Spec §9 — the toast copy for each provider failure. Anything unlisted reads as unreachable.
PROVIDER_MESSAGES = {
    "not_configured": "Flight verification isn't configured — add AVIATIONSTACK_API_KEY.",
    "invalid_key": "Flight service rejected our API key.",
    "plan": "Your aviationstack plan doesn't include this lookup.",
    "rate_limited": (
        "Flight service is busy — aviationstack allows one lookup every 10 seconds, "
        "try again in a moment."
    ),
    "quota": "Monthly flight lookups are used up.",
}
UNREACHABLE = "Couldn't reach the flight service — try again."


def _alert_la_stale(reservation: Reservation) -> None:
    """The trip was already pushed to LA — flag that LA needs a manual update."""
    if not reservation.la_reservation_id:
        return
    Notification.notify(
        reservation.lead,
        Notification.Kind.LA_CHANGED,
        title="Update LimoAnywhere",
        detail=f"Trip #{reservation.pk} changed after LA sync — edit it in LimoAnywhere.",
    )


@login_required
@require_POST
def reservation_save(request) -> HttpResponse:
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("invalid JSON")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid payload")
    lead = get_object_or_404(Lead, pk=payload.get("lead_id"))
    instance = None
    rid = payload.get("id")
    if rid not in (None, "") and str(rid).isdigit():
        instance = lead.reservations.filter(pk=rid).first()
    editing_existing = instance is not None
    try:
        save_reservation_from_draft(lead, payload, instance=instance)
    except DraftError as exc:
        return HttpResponseBadRequest(str(exc))
    if editing_existing:
        _alert_la_stale(instance)
    return redirect("lead_detail", pk=lead.pk)


@login_required
@require_POST
def reservation_duplicate(request, pk) -> HttpResponse:
    res = get_object_or_404(Reservation.objects.select_related("lead"), pk=pk)
    stops = list(res.stops.order_by("sequence"))
    last = res.lead.reservations.order_by("-sort_order").first()
    clone = Reservation.objects.get(pk=pk)
    clone.pk = None
    clone.service = f"{res.service or 'Reservation'} (copy)"
    clone.la_reservation_id = ""
    clone.trip_status = ""
    clone.revenue_status = Reservation.RevenueStatus.DEFERRED
    clone.recognized_at = None
    clone.recognized_amount = 0
    clone.sort_order = (last.sort_order + 1) if last else 0
    clone.save()
    Stop.objects.bulk_create(
        [
            Stop(reservation=clone, sequence=s.sequence, address=s.address, note=s.note)
            for s in stops
        ]
    )
    return redirect("lead_detail", pk=res.lead_id)


@login_required
@require_POST
def reservation_delete(request, pk) -> HttpResponse:
    res = get_object_or_404(Reservation, pk=pk)
    lead_id = res.lead_id
    _alert_la_stale(res)
    # The trip is about to stop existing — pull any affiliate offer first, so coverage is
    # released through the one door instead of vanishing with the CASCADE.
    dispatch_services.release_trips([res], note="Trip removed")
    res.delete()
    return redirect("lead_detail", pk=lead_id)


def _bad(message: str, code: str) -> JsonResponse:
    return JsonResponse({"error": message, "code": code}, status=400)


def _pk(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@login_required
@require_POST
def flight_verify(request) -> JsonResponse:
    """Check one flight against aviationstack (through the cache) and return its pill.

    Serves the editor (values from an unsaved draft) and the drawer (values from a saved
    stop) alike — the stop→flight link is derived on the next save, never written here.
    """
    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return _bad("invalid JSON", "bad_request")
    if not isinstance(payload, dict):
        return _bad("invalid payload", "bad_request")
    airport = Airport.objects.filter(pk=_pk(payload.get("airport"))).first()
    if airport is None:
        return _bad("Choose an airport from the list.", "airport")
    airline = Airline.objects.filter(pk=_pk(payload.get("airline"))).first()
    if airline is None:
        return _bad("Choose the airline first.", "airline")
    flight_number = str(payload.get("flight") or "").strip()
    if not _FLIGHT_RE.match(flight_number):
        return _bad("Enter the flight number (digits only).", "flight")
    direction = str(payload.get("direction") or "")
    if direction not in FlightDirection.values:
        return _bad("Choose Arriving or Departing to verify.", "direction")
    try:
        flight_date = date.fromisoformat(str(payload.get("date") or ""))
    except ValueError:
        return _bad("Set the trip date first.", "date")
    try:
        preferred = time.fromisoformat(str(payload.get("time") or ""))
    except ValueError:
        preferred = None
    try:
        flight = flights.lookup(
            airline=airline,
            flight_number=flight_number,
            airport=airport,
            direction=direction,
            flight_date=flight_date,
            preferred_time=preferred,
        )
    except FlightLookupError as exc:
        return _bad(exc.message, exc.code)
    except AviationstackError as exc:
        log.warning("aviationstack %s: %s", exc.code, exc.message)
        return JsonResponse(
            {"error": PROVIDER_MESSAGES.get(exc.code, UNREACHABLE), "code": exc.code},
            status=503,
        )
    return JsonResponse(flight.pill())
