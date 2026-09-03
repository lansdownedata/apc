"""Reservation editor endpoints — JSON-draft save, duplicate, delete, flight verify."""

from __future__ import annotations

import json
import logging
from datetime import date, time

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from apps.addresses.models import Airline, Airport
from apps.dispatch import services as dispatch_services
from apps.integrations.aviationstack import AviationstackError
from apps.leads.models import Lead
from apps.notifications.models import Notification

from . import flights
from .drafts import FLIGHT_RE, TAIL_RE, DraftError, save_reservation_from_draft
from .flights import FlightLookupError
from .groups import DUPLICATE_MAX, apply_to_group, clone_reservation, delete_group, set_group_size
from .models import FlightDirection, Reservation
from .routing import reverse_route

log = logging.getLogger(__name__)

# Spec §9 — the toast copy for each provider failure. Anything unlisted reads as unreachable.
PROVIDER_MESSAGES = {
    "not_configured": "Flight verification isn't configured — add AVIATIONSTACK_API_KEY.",
    "invalid_key": "Flight service rejected our API key.",
    "plan": "Your aviationstack plan doesn't include this lookup.",
    "rate_limited": "Flight service is busy — please try again in a moment.",
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
        saved = save_reservation_from_draft(lead, payload, instance=instance)
    except DraftError as exc:
        return HttpResponseBadRequest(str(exc))
    if editing_existing:
        _alert_la_stale(instance)
    # "Apply to all in group" first, so the copies a bigger quantity is about to make are
    # cloned from a reservation that already agrees with its siblings.
    if payload.get("applyToGroup"):
        for sibling in apply_to_group(saved):
            _alert_la_stale(sibling)
    quantity = _quantity(payload)
    if quantity is not None:
        set_group_size(saved, quantity)
    return redirect("lead_detail", pk=lead.pk)


def _quantity(payload: dict) -> int | None:
    """The editor's vehicle quantity, or None to leave the set exactly as it is.

    Absent-or-unparseable deliberately means "don't touch the size" rather than 1: a
    payload from a surface with no quantity control — an older client, the wedding
    builder — would otherwise silently collapse a set of four coaches down to one.
    """
    value = payload.get("quantity")
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _duplicate_count(request) -> int:
    """How many copies to make: 1..DUPLICATE_MAX, and 1 for anything unparseable."""
    try:
        n = int(request.POST.get("count", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, DUPLICATE_MAX))


@login_required
@require_POST
def reservation_duplicate(request, pk) -> HttpResponse:
    res = get_object_or_404(Reservation.objects.select_related("lead"), pk=pk)
    stops = list(res.stops.order_by("sequence"))
    last = res.lead.reservations.order_by("-sort_order").first()
    next_order = (last.sort_order + 1) if last else 0
    with transaction.atomic():
        for offset in range(_duplicate_count(request)):
            # No `group_key`: a duplicate is an independent trip. Linking a set is what
            # the editor's quantity field does (APC-14).
            clone_reservation(res, next_order + offset, stops=stops)
    return redirect("lead_detail", pk=res.lead_id)


@login_required
@require_POST
def reservation_reverse(request, pk) -> HttpResponse:
    """Flip a saved itinerary end-for-end (APC-16) — pickup ↔ drop-off, no clone.

    A linked set (APC-14) reverses as a whole so its members stay identical trips.
    """
    res = get_object_or_404(Reservation.objects.select_related("lead"), pk=pk)
    reverse_route(res, propagate=True)
    _alert_la_stale(res)
    if res.group_key is not None:
        for sibling in res.lead.reservations.exclude(pk=res.pk).filter(group_key=res.group_key):
            _alert_la_stale(sibling)
    return redirect("lead_detail", pk=res.lead_id)


@login_required
@require_POST
def reservation_group_delete(request, pk) -> HttpResponse:
    """Remove a whole linked set (APC-14) — the quote workspace shows one as one line.

    `reservation_delete`'s single-trip door still exists for a member removed from inside
    an expanded set; this is the line-level one.
    """
    res = get_object_or_404(Reservation, pk=pk)
    lead_id = res.lead_id
    _alert_la_stale(res)
    delete_group(res)
    return redirect("lead_detail", pk=lead_id)


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
    stop) alike. The editor's payload carries no `stop` id — its stop→flight link is
    derived on the next save (`flights.link_flights`), never written here, because the
    draft it verifies may have no saved Stop at all. The drawer's payload does carry
    `stop` (`Stop.flight_verify_payload`) since it has no editor save path back — when
    present, `flights.link_stop` links that one stop, guarded against a stale drawer.
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
    # A tail number's shape (TAIL_RE) only applies to the Private carrier — checked here
    # so a real one reaches `flights.lookup` below, whose own `private_flight` refusal is
    # the actual, clearer reason ("private flights aren't in any airline's schedule"), not
    # a misleading "digits only" one.
    pattern = TAIL_RE if airline.is_private else FLIGHT_RE
    if not pattern.match(flight_number.upper() if airline.is_private else flight_number):
        message = (
            "Enter the tail number (e.g. N561FX)."
            if airline.is_private
            else "Enter the flight number (digits only)."
        )
        return _bad(message, "flight")
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
    stop_id = _pk(payload.get("stop"))
    if stop_id is not None:
        flights.link_stop(stop_id, flight)
    return JsonResponse(flight.pill())
