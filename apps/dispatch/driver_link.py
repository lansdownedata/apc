"""Driver trip link rules: when a link is live and when its status window is open."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.dispatch.models import Assignment, DriverPing
from apps.notifications.models import Notification
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_circling, set_trip_status

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
    if pickup is None:
        return None
    # Subtract in UTC: timedelta math on a zone-aware datetime is wall-clock, which drifts
    # an hour on a DST night. The result is still expressed in the trip's zone.
    return (pickup.astimezone(UTC) - WINDOW_BEFORE_PICKUP).astimezone(pickup.tzinfo)


def window_open(trip: Reservation, *, now: datetime | None = None) -> bool:
    """False for a trip with no pickup date: the window never opens."""
    opens = window_opens_at(trip)
    return opens is not None and (now or timezone.now()) >= opens


class DriverLinkError(Exception):
    """Base for a driver tap the link refuses."""


class DeadLink(DriverLinkError):
    """The link no longer belongs to live coverage."""


class StaleStatus(DriverLinkError):
    """The driver's page was behind the trip's real status."""

    def __init__(self, current: str):
        super().__init__(current)
        self.current = current


class NotAllowed(DriverLinkError):
    """The tap is not a legal step from the trip's current status."""

    def __init__(self, current: str):
        super().__init__(current)
        self.current = current


class BeforeWindow(DriverLinkError):
    """The status window has not opened yet."""

    def __init__(self, opens_at: datetime | None):
        super().__init__(opens_at)
        self.opens_at = opens_at


def next_status(current: str) -> str | None:
    if current not in SEQUENCE:
        return None
    index = SEQUENCE.index(current)
    return SEQUENCE[index + 1] if index + 1 < len(SEQUENCE) else None


def back_status(current: str) -> str | None:
    if current not in SEQUENCE:
        return None
    index = SEQUENCE.index(current)
    if index == 0 or current == _S.DONE:
        return None
    return SEQUENCE[index - 1]


def _lock(assignment_pk: int) -> tuple[Assignment, Reservation]:
    """Re-read the assignment, then its trip, under row locks. Call inside atomic()."""
    assignment = Assignment.objects.select_for_update().get(pk=assignment_pk)
    trip = Reservation.objects.select_for_update().get(pk=assignment.reservation_id)
    assignment.reservation = trip
    return assignment, trip


def _valid_coords(coords: dict | None) -> bool:
    if not coords:
        return False
    try:
        return -90 <= float(coords["lat"]) <= 90 and -180 <= float(coords["lng"]) <= 180
    except (KeyError, TypeError, ValueError):
        return False


def apply_status(
    assignment_pk: int,
    *,
    to: str,
    from_: str,
    coords: dict | None = None,
    now: datetime | None = None,
) -> TripStatusEvent:
    """One driver tap: move the trip one step forward or back, if the page was current."""
    now = now or timezone.now()
    with transaction.atomic():
        assignment, trip = _lock(assignment_pk)
        if not is_live(assignment, now=now):
            raise DeadLink
        if not window_open(trip, now=now):
            raise BeforeWindow(window_opens_at(trip))
        if from_ != trip.trip_status:
            raise StaleStatus(trip.trip_status)
        if to not in (next_status(trip.trip_status), back_status(trip.trip_status)):
            raise NotAllowed(trip.trip_status)
        event = set_trip_status(
            trip, to, source=TripStatusEvent.Source.DRIVER_LINK, assignment=assignment
        )
        if event is not None and _valid_coords(coords):
            DriverPing.objects.create(
                assignment=assignment,
                reservation=trip,
                kind=DriverPing.Kind.STATUS,
                status_event=event,
                latitude=coords["lat"],
                longitude=coords["lng"],
                accuracy_m=coords.get("accuracy"),
                recorded_at=now,
            )
    return event


def set_driver_circling(assignment_pk: int, *, on: bool) -> None:
    with transaction.atomic():
        assignment, trip = _lock(assignment_pk)
        if not is_live(assignment):
            raise DeadLink
        try:
            set_circling(trip, on=on, source=Reservation.CirclingSource.DRIVER_LINK)
        except ValueError:
            raise NotAllowed(trip.trip_status) from None


def report_no_show(assignment_pk: int, *, note: str = "") -> None:
    """Record the driver's no-show report and tell dispatch. The trip status is unchanged."""
    with transaction.atomic():
        assignment, trip = _lock(assignment_pk)
        if not is_live(assignment):
            raise DeadLink
        trip.driver_no_show_at = timezone.now()
        trip.driver_no_show_note = note[:500]
        trip.save(update_fields=["driver_no_show_at", "driver_no_show_note", "updated_at"])
        Notification.notify(
            trip.lead,
            Notification.Kind.DRIVER_REPORT,
            title=f"Driver reports no-show on trip #{trip.reference}",
            detail=note[:255],
        )
