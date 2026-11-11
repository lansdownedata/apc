"""The driver's trip page and its JSON endpoints (public; the signed token is the credential).

No login. A forged token 404s; a link that is no longer live shows only the dispatch phone
line. The page never carries money: nothing here reads a payout, price or cost.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.core.signing import BadSignature
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.dispatch import driver_link
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.reservations import acknowledgements as ack
from apps.reservations.models import Reservation, trip_status_label
from apps.reservations.routes import route_for
from apps.reservations.services import flight_line

_S = Reservation.TripStatus

BUTTON_LABELS = {
    _S.DISPATCHED: "I'm on the way",
    _S.ON_THE_WAY: "I've arrived",
    _S.ARRIVED: "Passenger on board",
    _S.CUSTOMER_IN_CAR: "Trip complete",
}
WAITING = "Waiting on dispatch"
NO_PICKUP = "Pickup time not set — call dispatch."
_CIRCLING_STATUSES = (_S.ON_THE_WAY, _S.ARRIVED)
_PICKUP_NEXT = (_S.DISPATCHED, _S.ON_THE_WAY, _S.ARRIVED)


def _driver_headers(view):
    """Stamp the three privacy headers on every response from a driver-link view."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "no-referrer"
        response["X-Robots-Tag"] = "noindex"
        return response

    return wrapper


def _load(token: str) -> Assignment | None:
    try:
        assignment = ack.read_driver_link_token(token)
    except (BadSignature, Assignment.DoesNotExist):
        return None
    return Assignment.objects.select_related(
        "reservation",
        "reservation__lead",
        "reservation__lead__contact",
        "reservation__vehicle",
        "reservation__service_type",
        "driver",
        "vendor",
    ).get(pk=assignment.pk)


def _not_found() -> HttpResponse:
    return HttpResponse("Not found", status=404)


def _time_label(moment: datetime | None) -> str:
    return f"{moment:%-I:%M %p} {moment.tzname()}" if moment else ""


def page_state(assignment: Assignment, *, now: datetime | None = None) -> dict:
    """What the page needs to draw itself: also the body of every POST response."""
    trip = assignment.reservation
    status = trip.trip_status
    in_sequence = status in driver_link.SEQUENCE
    nxt = driver_link.next_status(status)
    opens = driver_link.window_opens_at(trip)
    message = ""
    if not in_sequence:
        message = WAITING
    elif opens is None:
        message = NO_PICKUP
    return {
        "status": status,
        "status_label": trip_status_label(status),
        "step": driver_link.SEQUENCE.index(status) if in_sequence else -1,
        "next": {"to": nxt, "label": BUTTON_LABELS[status]} if nxt else None,
        "back": driver_link.back_status(status),
        "window_open": driver_link.window_open(trip, now=now),
        "opens_at_label": _time_label(opens),
        "can_circle": status in _CIRCLING_STATUSES,
        "circling": trip.circling_at is not None,
        "share_location": status in driver_link.PING_STATUSES,
        "done": status == _S.DONE,
        "message": message,
    }


def _stop_dicts(trip: Reservation, stops: list, user_agent: str) -> list[dict]:
    """Each stop as the page draws it. `navigate_url` rides on every stop so the page can
    swap the next-stop card from pickup to drop-off without a reload."""
    zone = trip.pickup_tz_label
    last = len(stops)
    rows = []
    for n, stop in enumerate(stops, start=1):
        clock = stop.scheduled_time or (trip.pickup_time if n == 1 else None)
        row = {
            "n": n,
            "kind": "Pickup" if n == 1 else "Drop-off" if n == last else "Stop",
            "time_label": f"{clock:%-I:%M %p} {zone}" if clock else "",
            "name": stop.name,
            "address": stop.address,
            "note": stop.note,
            "flight": flight_line(stop),
            "lat": str(stop.latitude) if stop.latitude is not None else None,
            "lng": str(stop.longitude) if stop.longitude is not None else None,
        }
        row["navigate_url"] = _navigate_url(row, user_agent)
        rows.append(row)
    return rows


def _navigate_url(stop: dict | None, user_agent: str) -> str:
    if stop is None:
        return ""
    has_coords = stop["lat"] is not None and stop["lng"] is not None
    where = f"{stop['lat']},{stop['lng']}" if has_coords else stop["address"]
    if not where:
        return ""
    if "iPhone" in user_agent or "iPad" in user_agent:
        return f"https://maps.apple.com/?daddr={quote(where, safe=',')}"
    return f"https://www.google.com/maps/dir/?api=1&destination={quote(where, safe=',')}"


def page_context(assignment: Assignment, request: HttpRequest) -> dict:
    trip = assignment.reservation
    state = page_state(assignment)
    stop_rows = list(trip.ordered_stops)
    stops = _stop_dicts(trip, stop_rows, request.META.get("HTTP_USER_AGENT", ""))
    next_stop = None
    if stops:
        heading_to_pickup = state["status"] not in (_S.CUSTOMER_IN_CAR, _S.DONE)
        next_stop = dict(stops[0] if heading_to_pickup else stops[-1])
        next_stop["eyebrow"] = "Next · Pickup" if heading_to_pickup else "Next · Drop-off"
    show_contact = (
        assignment.is_in_house or DispatchAlertConfig.load().driver_page_show_passenger_contact
    )
    named = list(trip.named_passengers.all())
    lead_passenger = next((p for p in named if p.is_lead), None)
    map_key = settings.LOCATIONIQ_PUBLIC_KEY
    has_route = (
        bool(map_key)
        and bool(stop_rows)
        and all(s.latitude is not None and s.longitude is not None for s in stop_rows)
    )
    return {
        "state": state,
        "trip_ref": trip.reference,
        "trip_date": f"{trip.pickup_date:%a, %b %-d}" if trip.pickup_date else "",
        "stops": stops,
        "next_stop": next_stop,
        "flight_lines": [s["flight"] for s in stops if s["flight"]],
        "passengers": [p.name for p in named if p.name],
        "pax_count": trip.passengers,
        "vehicle": trip.vehicle.name if trip.vehicle else "",
        "sign_name": (lead_passenger.name if lead_passenger else "") or trip.lead.contact.name,
        "show_passenger_contact": show_contact,
        "lead_mobile": lead_passenger.mobile if show_contact and lead_passenger else "",
        "navigate_url": next_stop["navigate_url"] if next_stop else "",
        "company_phone": settings.COMPANY_PHONE,
        "map_key": map_key,
        "route": route_for(trip) if has_route else "",
    }


def _client_config(request: HttpRequest, ctx: dict, token: str) -> dict:
    """The one JSON blob the page script reads. Never holds the server geocoding key."""
    url = lambda name: reverse(name, args=[token])  # noqa: E731
    return {
        "state": ctx["state"],
        "stops": ctx["stops"],
        "map_key": ctx["map_key"],
        "route": ctx["route"],
        "csrf": get_token(request),
        "urls": {
            "status": url("driver_link_status"),
            "circling": url("driver_link_circling"),
            "no_show": url("driver_link_no_show"),
            "ping": url("driver_link_ping"),
        },
    }


@_driver_headers
@require_GET
def driver_link_page(request: HttpRequest, token: str) -> HttpResponse:
    assignment = _load(token)
    if assignment is None:
        return _not_found()
    if not driver_link.is_live(assignment):
        ctx = {"dead": True, "company_phone": settings.COMPANY_PHONE}
    else:
        ctx = page_context(assignment, request)
        ctx["config"] = _client_config(request, ctx, token)
    # The shell's og:url is the request URL, which here carries the token: point it home.
    ctx["canonical_url"] = request.build_absolute_uri("/")
    return render(request, "public/driver_link.html", ctx)


def _json_body(request: HttpRequest) -> dict | None:
    """The POST body as a dict, or None when it is not a JSON object."""
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _state_response(assignment_pk: int, *, status: int = 200, **extra) -> JsonResponse:
    fresh = Assignment.objects.select_related("reservation").get(pk=assignment_pk)
    return JsonResponse({"state": page_state(fresh), **extra}, status=status)


def _driver_post(view):
    """Shared plumbing for the state-changing endpoints: token, JSON body, refused taps."""

    @_driver_headers
    @require_POST
    @wraps(view)
    def wrapper(request: HttpRequest, token: str) -> HttpResponse:
        assignment = _load(token)
        if assignment is None:
            return _not_found()
        body = _json_body(request)
        if body is None:
            return JsonResponse({"error": "Bad request"}, status=400)
        try:
            return view(request, assignment, body)
        except driver_link.DeadLink:
            return JsonResponse({"dead": True}, status=410)
        except (driver_link.StaleStatus, driver_link.NotAllowed):
            return _state_response(assignment.pk, status=409)
        except driver_link.BeforeWindow as exc:
            opens = _time_label(exc.opens_at)
            message = f"Opens at {opens}, 6 hours before pickup" if opens else "Not open yet"
            return _state_response(assignment.pk, status=400, error=message)
        except driver_link.BadCoordinates:
            return JsonResponse({"error": "Bad position"}, status=400)

    return wrapper


@_driver_post
def driver_link_status(request: HttpRequest, assignment: Assignment, body: dict) -> HttpResponse:
    coords = {k: body.get(k) for k in ("lat", "lng", "accuracy")} if "lat" in body else None
    driver_link.apply_status(
        assignment.pk,
        to=str(body.get("to") or ""),
        from_=str(body.get("from") or ""),
        coords=coords,
    )
    return _state_response(assignment.pk)


@_driver_post
def driver_link_circling(request: HttpRequest, assignment: Assignment, body: dict) -> HttpResponse:
    driver_link.set_driver_circling(assignment.pk, on=bool(body.get("on")))
    return _state_response(assignment.pk)


@_driver_post
def driver_link_no_show(request: HttpRequest, assignment: Assignment, body: dict) -> HttpResponse:
    note = body.get("note")
    driver_link.report_no_show(assignment.pk, note=note if isinstance(note, str) else "")
    return _state_response(assignment.pk)


def _recorded_at(value) -> datetime | None:
    """The browser's ISO 8601 timestamp as an aware datetime, or None when it is missing,
    unparseable or carries no offset (a naive value cannot be placed in time)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = parse_datetime(value)
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.utcoffset() is not None else None


@_driver_headers
@require_POST
def driver_link_ping(request: HttpRequest, token: str) -> HttpResponse:
    """Periodic position. 204 whether or not it was stored, so a dead link learns nothing."""
    assignment = _load(token)
    if assignment is None:
        return _not_found()
    body = _json_body(request)
    if body is None:
        return JsonResponse({"error": "Bad request"}, status=400)
    try:
        driver_link.record_ping(
            assignment.pk,
            lat=body.get("lat"),
            lng=body.get("lng"),
            accuracy=body.get("accuracy"),
            speed=body.get("speed"),
            heading=body.get("heading"),
            recorded_at=_recorded_at(body.get("recorded_at")),
        )
    except driver_link.BadCoordinates:
        return JsonResponse({"error": "Bad position"}, status=400)
    return HttpResponse(status=204)
