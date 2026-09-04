"""Reservation-level services — trip-sheet rendering + trip-status transitions.

The trip sheet is one shared shape (`trip_sheet_context`) rendered two ways: the public
acknowledgement pages (`components/trip_sheet.html`) and the plain-text `{trip_sheet}` /
`{trip_routing}` touch-point vars (`trip_sheet_text`).

`templates/email/vendor_offer.{txt,html}` deliberately stays on its own shape and is NOT
built on this (the 2026-09-04 spec assumed it would be — it shouldn't). Two reasons, both
regressions if it were forced onto this: its payout is the *offer's* (`assignment.payout`
on an OFFERED row), where this helper reads the *confirmed* assignment and would render
blank mid-offer; and it carries per-stop verified-flight detail — scheduled/actual time,
zone, terminal — that an offer needs and a summary sheet doesn't reduce to. What the two
do share is that detail's shape, so `flight_line()` below is the seam, not the template.
The offer templates still assemble that detail inline (they interleave it with each stop's
address, which this flat list can't express) — change one and change the other.
"""

from __future__ import annotations

from .models import Flight, FlightDirection, Reservation, Stop, TripStatusEvent


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


def flight_line(stop: Stop) -> str:
    """One stop's flight detail, as far as it's verified: `"UA 123 · arr 7:30 PM EDT ·
    Terminal C"`, falling back to just the label when nothing has been looked up yet.
    Blank for a stop with no flight at all.

    A cancelled / diverted / incident flight says so *instead of* a time: its
    `scheduled_at` survives the cancellation, so rendering the time would tell an
    affiliate to go meet a plane that isn't coming. Same rule the pill follows.

    The shape the affiliate offer email has carried since the flight-verification work
    (2026-08-29) — an affiliate meeting a plane needs the time and terminal, not just a
    flight number. Shared so the T-48h confirmation sheet says the same thing the offer
    did; the offer email's own templates render it inline and stay as they are.

    Times come from the *airport's* zone via `Flight.time_local` / `tz_abbr`, never the
    trip's and never the viewer's — a DCA→LAX leg's flight clock is LA's, not the trip's.
    """
    label = stop.flight_label
    if not label:
        return ""
    flight = stop.flight if stop.flight_id else None
    if flight is None:
        return label
    parts = [label]
    if flight.pill_state == "cancelled":
        parts.append("Diverted" if flight.status == Flight.Status.DIVERTED else "Cancelled")
    elif flight.best_at:
        verb = "dep" if stop.flight_direction == FlightDirection.DEPARTURE else "arr"
        parts.append(f"{verb} {flight.time_local} {flight.tz_abbr}".strip())
        if flight.terminal:
            parts.append(f"Terminal {flight.terminal}")
    return " · ".join(parts)


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
        # Pre-rendered per stop so the HTML partial and the plain-text build read the
        # same strings rather than each assembling flight detail their own way.
        "flight_lines": [line for s in stops if (line := flight_line(s))],
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
    for line in ctx["flight_lines"]:
        lines.append(f"  Flight: {line}")
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
