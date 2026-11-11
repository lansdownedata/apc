"""Driver trip link rules: when a link is live and when its status window is open."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from apps.dispatch.models import Assignment
from apps.reservations.models import Reservation, TripStatusEvent

_S = Reservation.TripStatus

SEQUENCE = (_S.DISPATCHED, _S.ON_THE_WAY, _S.ARRIVED, _S.CUSTOMER_IN_CAR, _S.DONE)
WINDOW_BEFORE_PICKUP = timedelta(hours=6)
DONE_GRACE = timedelta(hours=12)


def done_at(trip: Reservation) -> datetime | None:
    """When the trip last reached Done, or None. Counts only while the trip is still Done,
    so one that dispatch moved back out of Done has no clock running."""
    if trip.trip_status != _S.DONE:
        return None
    return (
        TripStatusEvent.objects.filter(reservation=trip, status=_S.DONE)
        .order_by("-created_at", "-pk")
        .values_list("created_at", flat=True)
        .first()
    )


def is_live(assignment: Assignment, *, now: datetime | None = None) -> bool:
    """A link is live only for confirmed coverage on a trip that is not cancelled and not
    long Done. Reassignment creates a new assignment, so the old link dies with its row."""
    now = now or timezone.now()
    if assignment.status != Assignment.Status.CONFIRMED:
        return False
    trip = assignment.reservation
    if trip.is_cancelled:
        return False
    finished = done_at(trip)
    return finished is None or now <= finished + DONE_GRACE


def window_opens_at(trip: Reservation) -> datetime | None:
    pickup = trip.pickup_at
    return pickup - WINDOW_BEFORE_PICKUP if pickup else None


def window_open(trip: Reservation, *, now: datetime | None = None) -> bool:
    """False for a trip with no pickup date: the window never opens."""
    opens = window_opens_at(trip)
    return opens is not None and (now or timezone.now()) >= opens
