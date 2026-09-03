"""Reversing a saved itinerary end-for-end (APC-16).

`reverse_route` rewrites `Stop.sequence` in place — no clone, no new reservation. It is
the shared helper the "Create Return Trip" flow (APC-15) builds on: clone first, then
reverse the copy.
"""

from __future__ import annotations

from django.db import transaction

from .models import FlightDirection, Reservation, Stop


@transaction.atomic
def reverse_route(reservation: Reservation, *, propagate: bool = False) -> Reservation:
    """Flip `reservation`'s stops end-for-end: the pickup becomes the drop-off and back.

    - `Stop.sequence` is reversed; every other column stays with its stop.
    - Flight direction is re-derived for the two new endpoints (a pickup meets an
      *arrival*, a drop-off catches a *departure* — `drafts._apply_flight_directions`);
      a middle airport stop keeps the side the agent chose.
    - A flight cache row on a stop whose direction flipped is dropped — it answered a
      question ("UA 123 arriving at IAD") that no longer applies.
    - Intermediate `scheduled_time`s are cleared (they would otherwise run backwards);
      the endpoints take the trip's own pickup / drop-off time, matching
      `drafts._derive_endpoint_stop_times`.

    `propagate=True` fans the reversed route onto the rest of a linked set (APC-14) so a
    "×4" coach group stays four identical trips.
    """
    stops = list(reservation.stops.order_by("sequence"))
    last = len(stops) - 1
    for i, stop in enumerate(stops):
        stop.sequence = last - i

    for stop in stops:
        before = stop.flight_direction
        if stop.airport_id is None:
            stop.flight_direction = ""
        elif stop.sequence == 0:
            stop.flight_direction = FlightDirection.ARRIVAL
        elif stop.sequence == last:
            stop.flight_direction = FlightDirection.DEPARTURE
        # a middle airport stop keeps its chosen side
        if stop.flight_direction != before:
            stop.flight_id = None

        if stop.sequence == 0:
            stop.scheduled_time = reservation.pickup_time
        elif stop.sequence == last:
            stop.scheduled_time = reservation.dropoff_time
        else:
            stop.scheduled_time = None

    Stop.objects.bulk_update(stops, ["sequence", "flight_direction", "flight_id", "scheduled_time"])
    reservation.refresh_pickup_timezone()

    if propagate and reservation.group_key is not None:
        from . import groups

        groups.apply_to_group(reservation)

    return reservation
