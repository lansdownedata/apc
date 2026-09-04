"""Reservation-level services — trip-sheet rendering + trip-status transitions.

The trip sheet is one shared shape (`trip_sheet_context`) rendered three ways: the public
acknowledgement pages (`components/trip_sheet.html`), the plain-text `{trip_sheet}` /
`{trip_routing}` touch-point vars (`trip_sheet_text`), and the affiliate offer email.
"""

from __future__ import annotations

from .models import Reservation, TripStatusEvent


def is_wedding_trip(reservation: Reservation) -> bool:
    """A leg the wedding builder generated, or a trip whose service type is the wedding
    one (`apps.public.services.WEDDING_SERVICE_NAME`). The single gate for "is this a
    wedding" — the T-7d touch-point (APC-18) and the workspace's day-of-details card both
    need it, and drifting between two copies would mean scheduling a message for a trip
    the screen doesn't think is a wedding, or the reverse.
    """
    from apps.public.services import WEDDING_SERVICE_NAME

    if reservation.source_leg_id:
        return True
    st = reservation.service_type
    return bool(st and st.name.strip().lower() == WEDDING_SERVICE_NAME.lower())


def trip_sheet_context(reservation: Reservation, *, affiliate: bool = False) -> dict:
    """The one trip-sheet shape. Times render in the trip's own timezone with its
    abbreviation (CLAUDE.md rules), never the viewer's. ``affiliate`` adds the payout row.
    """
    pickup_at = reservation.pickup_at
    stops = list(reservation.ordered_stops)
    lead = reservation.lead
    ctx: dict = {
        "quote_no": lead.quote_no,
        "pickup_date": f"{pickup_at:%A, %B %-d, %Y}" if pickup_at else "",
        "pickup_time": (
            f"{pickup_at:%-I:%M %p} {reservation.pickup_tz_abbrev}".strip()
            if pickup_at and reservation.pickup_time is not None
            else ""
        ),
        "routing": [s.address for s in stops if s.address],
        "passengers": reservation.passengers,
        "vehicle": (
            (reservation.vehicle.name if reservation.vehicle else "")
            or (reservation.service_type.name if reservation.service_type else "")
        ),
        "flight": reservation.flight_summary,
        "stops": stops,
    }
    if affiliate:
        from apps.dispatch.selectors import confirmed_assignment

        confirmed = confirmed_assignment(reservation)
        ctx["payout"] = f"${confirmed.payout:,.2f}" if confirmed else ""
    return ctx


def trip_sheet_text(reservation: Reservation) -> str:
    """Plain-text trip sheet for the SMS/email `{trip_sheet}` variable."""
    ctx = trip_sheet_context(reservation)
    lines = [f"Trip {ctx['quote_no']}"]
    if ctx["pickup_date"]:
        when = ctx["pickup_date"]
        if ctx["pickup_time"]:
            when = f"{when} at {ctx['pickup_time']}"
        lines.append(f"  {when}")
    if ctx["routing"]:
        lines.append(f"  Route: {' → '.join(ctx['routing'])}")
    lines.append(f"  Passengers: {ctx['passengers']}")
    if ctx["vehicle"]:
        lines.append(f"  Vehicle: {ctx['vehicle']}")
    for stop in ctx["stops"]:
        label = stop.flight_label
        if label:
            lines.append(f"  Flight: {label}")
    return "\n".join(lines)


def set_trip_status(
    reservation: Reservation,
    status: str,
    *,
    user=None,
    source: str = TripStatusEvent.Source.MANUAL,
) -> TripStatusEvent | None:
    """Write a trip status, log the transition, and fire the customer notification hook.

    The single seam every status change routes through (LA webhook + the dispatch drawer),
    so the APC-22 message hook has one home. Returns the event, or None when the status is
    unchanged.
    """
    if reservation.trip_status == status:
        return None
    reservation.trip_status = status
    reservation.save(update_fields=["trip_status", "updated_at"])
    event = TripStatusEvent.objects.create(
        reservation=reservation, status=status, source=source, changed_by=user
    )
    from apps.messaging.touchpoints import notify_status_change

    notify_status_change(reservation, status)
    return event
