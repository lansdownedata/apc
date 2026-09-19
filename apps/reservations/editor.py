"""Read side of the trip editor: what one screen has to hand it to open.

The write side is `drafts.py` (parse a posted draft, save it). This is its inverse plus
the pickers the modal needs, in one place because three screens now open the same editor —
the quote workspace, the order page, and the dispatch drawer next. Building that context
per view is how they drift into offering different vehicles or a different set of stops.
"""

from __future__ import annotations

from apps.leads.models import Lead, VehicleType

from . import groups


def reservation_draft(r, *, quantity: int = 1) -> dict:
    """The editor's view of one saved trip. `quantity` is the size of the linked set it
    belongs to (APC-14) — 1 for a trip that stands alone."""
    return {
        "id": r.pk,
        "quantity": quantity,
        "tripType": r.trip_type,
        "serviceType": r.service_type_id or "",
        "date": r.pickup_date.isoformat() if r.pickup_date else "",
        "time": r.pickup_time.strftime("%H:%M") if r.pickup_time else "",
        "vehicle": r.vehicle_id or "",
        "pax": r.passengers,
        "rate": float(r.rate),
        "hours": float(r.hours),
        "minHours": float(r.min_hours),
        "gratuityPct": float(r.gratuity_pct),
        "gratuityFlat": float(r.gratuity_flat),
        "discountPct": float(r.discount_pct),
        "discountFlat": float(r.discount_flat),
        "affiliateCost": float(r.affiliate_cost),
        "costRatioPct": float(r.cost_ratio_pct),
        "dropoffDate": r.dropoff_date.isoformat() if r.dropoff_date else "",
        "dropoffTime": r.dropoff_time.strftime("%H:%M") if r.dropoff_time else "",
        "stops": [
            {
                "address": s.address,
                "note": s.note,
                "name": s.name,
                "time": s.scheduled_time.strftime("%H:%M") if s.scheduled_time else "",
                # Round-tripped so the delete-and-recreate in save_reservation_from_draft
                # doesn't drop coordinates the user already picked.
                "lat": str(s.latitude) if s.latitude is not None else "",
                "lng": str(s.longitude) if s.longitude is not None else "",
                "airport": s.airport_id or "",
                "airportCode": s.airport.iata if s.airport_id else "",
                # Gates the editor's Verify button (spec 2026-08-29 finding 2) — a stop's
                # airport can have a real IATA code and still have no scheduled service
                # (Andrews, Manassas, ...).
                "hasScheduledService": bool(s.airport_id and s.airport.has_scheduled_service),
                "airline": s.airline_id or "",
                "flight": s.flight_number,
                "direction": s.flight_direction,
                # Pre-rendered pill for a stop already linked to a cached flight, so the
                # editor opens with the check shown. Client-only; the parser ignores it.
                "pill": s.flight_pill,
            }
            for s in r.stops.all()
        ],
    }


def open_editor_id(request, reservations) -> int | None:
    """The `?edit=<pk>` trip to reopen the editor on, if it names a real trip on this
    quote — a redirect from Create Return Trip (APC-15) uses it. Anything else is None."""
    raw = request.GET.get("edit", "")
    if not raw.isdigit():
        return None
    pk = int(raw)
    return pk if any(r.pk == pk for r in reservations) else None


def editor_context(request, lead: Lead | None, reservations, *, trip_defaults=None) -> dict:
    """Everything `leads/_reservation_editor.html` needs, for any screen that includes it.

    `reservations` must already be the page's prefetched list — the drafts read each
    trip's stops, so re-querying here would cost one round trip per row. Pass an empty one
    (and no lead) for a screen that spans several customers: the dispatch board hands the
    editor nothing up front and lets it fetch the trip being opened.
    """
    from apps.leads import services as lead_services

    vehicles = list(
        VehicleType.objects.filter(active=True).values(
            "id", "name", "rate", "hourly_min_hours", "transfer_min_hours"
        )
    )
    sizes = {m.pk: line.size for line in groups.as_lines(reservations) for m in line.members}
    return {
        "open_editor_id": open_editor_id(request, reservations),
        # What a new trip starts with. Only a wedding sets any — see leads.views.
        "trip_defaults": trip_defaults or {},
        "duplicate_max": groups.DUPLICATE_MAX,
        "reservations_json": [
            reservation_draft(r, quantity=sizes.get(r.pk, 1)) for r in reservations
        ],
        "vehicles_json": [
            {
                "id": v["id"],
                "name": v["name"],
                "rate": float(v["rate"]),
                "hourlyMin": float(v["hourly_min_hours"]),
                "transferMin": float(v["transfer_min_hours"]),
            }
            for v in vehicles
        ],
        "vehicle_options": [(v["id"], v["name"]) for v in vehicles],
        "service_type_options": lead_services.service_type_options(lead),
    }
