"""Driver trip link rules: when a link is live and when its status window is open, and the
messages that take the link to the driver."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.dispatch.models import Assignment, DriverPing
from apps.notifications.models import Notification
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import set_circling, set_trip_status

logger = logging.getLogger(__name__)

_S = Reservation.TripStatus

SEQUENCE = (_S.DISPATCHED, _S.ON_THE_WAY, _S.ARRIVED, _S.CUSTOMER_IN_CAR, _S.DONE)
WINDOW_BEFORE_PICKUP = timedelta(hours=6)
DONE_GRACE = timedelta(hours=12)
PING_STATUSES = (_S.ON_THE_WAY, _S.ARRIVED, _S.CUSTOMER_IN_CAR)
PING_MIN_GAP = timedelta(seconds=15)
PING_RETENTION = timedelta(days=90)


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


class BadCoordinates(DriverLinkError):
    """A ping carried a position or accuracy that cannot be real."""


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
    """Re-read the trip, then its assignment, under row locks. Call inside atomic().

    Reservation first, matching `services.send_back` and `_claim`, so no two paths can
    deadlock on the pair."""
    reservation_id = Assignment.objects.values_list("reservation_id", flat=True).get(
        pk=assignment_pk
    )
    trip = Reservation.objects.select_for_update().get(pk=reservation_id)
    assignment = Assignment.objects.select_for_update().get(pk=assignment_pk)
    assignment.reservation = trip
    return assignment, trip


def _finite(value) -> float | None:
    """The value as a finite float, or None when it is missing or not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_accuracy(value) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _valid_coords(coords: dict | None) -> bool:
    if not coords:
        return False
    lat, lng = _finite(coords.get("lat")), _finite(coords.get("lng"))
    return lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180


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
                accuracy_m=_clean_accuracy(coords.get("accuracy")),
                recorded_at=now,
            )
    return event


def record_ping(
    assignment_pk: int,
    *,
    lat,
    lng,
    accuracy=None,
    speed=None,
    heading=None,
    recorded_at: datetime | None = None,
    now: datetime | None = None,
) -> DriverPing | None:
    """Store one periodic position from the driver's page. Returns None, writing nothing,
    when the link is dead, the trip is not under way, or the last ping was under 15s ago."""
    latitude, longitude = _finite(lat), _finite(lng)
    if latitude is None or longitude is None:
        raise BadCoordinates("Position is not a number.")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise BadCoordinates("Position is out of range.")
    if accuracy is not None and _clean_accuracy(accuracy) is None:
        raise BadCoordinates("Accuracy must be zero or more.")
    now = now or timezone.now()
    with transaction.atomic():
        assignment, trip = _lock(assignment_pk)
        if not is_live(assignment, now=now) or trip.trip_status not in PING_STATUSES:
            return None
        last = (
            DriverPing.objects.filter(assignment=assignment)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        if last is not None and now - last < PING_MIN_GAP:
            return None
        return DriverPing.objects.create(
            assignment=assignment,
            reservation=trip,
            kind=DriverPing.Kind.TRACK,
            latitude=round(latitude, 6),
            longitude=round(longitude, 6),
            accuracy_m=_clean_accuracy(accuracy),
            speed_mps=_finite(speed),
            heading_deg=_finite(heading),
            recorded_at=recorded_at,
            created_at=now,
        )


def purge_driver_pings(*, now: datetime | None = None) -> int:
    """Delete pings older than the retention window; returns how many went."""
    now = now or timezone.now()
    deleted, _ = DriverPing.objects.filter(created_at__lt=now - PING_RETENTION).delete()
    return deleted


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


# --- the link's messages --------------------------------------------------------------

VENDOR_ROW = "affiliate_confirmed"
DRIVER_ROW = "driver_assignment"


def _has_address(assignment: Assignment) -> bool:
    """Somewhere to send the link: the vendor's email, or our driver's mobile or email."""
    if assignment.is_in_house:
        driver = assignment.driver
        return bool((driver.phone or "").strip() or (driver.email or "").strip())
    return bool((assignment.vendor.email or "").strip())


def _latest_touchpoint(assignment: Assignment):
    """The newest link message tried for this coverage, as (status, error), or None."""
    from apps.messaging.models import TouchPoint

    return (
        TouchPoint.objects.filter(
            reservation_id=assignment.reservation_id,
            kind__in=(VENDOR_ROW, DRIVER_ROW),
            created_at__gte=assignment.created_at,
        )
        .order_by("-created_at", "-pk")
        .values_list("status", "error")
        .first()
    )


def _skipped_by_choice(assignment: Assignment) -> bool:
    """The link message was not sent because Settings turned its row off or limited it to
    other service types: an admin's choice, so there is nothing to alert on."""
    from apps.messaging import rules

    latest = _latest_touchpoint(assignment)
    return latest is not None and latest[1] in (rules.DISABLED, rules.SERVICE_TYPE_MISMATCH)


def send_link(assignment: Assignment):
    """Send confirmed coverage its driver link: the "Trip confirmed" email to the vendor, or
    the "Driver trip assignment" text and email to our own driver. Returns the
    `sender.SendResult`, or None when there is nowhere to send it (no notice either: the
    drawer says "No email on file").

    Best effort, like the offer email: the coverage is already recorded, so a send that is
    refused (outbound off), fails or raises tells dispatch with a notification to copy the
    link from the drawer, and never fails the assignment. Nothing raises out of here. A row
    that Settings turned off, or limited to other service types, is the admin's choice and
    raises no notification."""
    from apps.messaging import rendering, sender
    from apps.reservations.acknowledgements import driver_link_url

    trip = assignment.reservation
    try:
        if not _has_address(assignment):
            return None
        values = {
            "driver_link": driver_link_url(assignment, base_url=settings.PUBLIC_BASE_URL or "")
        }
        blocks = {}
        if assignment.is_in_house:
            key = DRIVER_ROW
        else:
            from .services import _affiliate_values

            key = VENDOR_ROW
            values |= _affiliate_values(assignment.vendor, trip)
            # The plain trip sheet: no payout, no price, no money of any kind.
            blocks["trip_sheet"] = rendering.block(
                "trip_sheet", {"trip": trip, "stops": list(trip.ordered_stops)}
            )
        result = sender.send_now(
            key, trip.lead, trip, assignment=assignment, extra_ctx=values, blocks=blocks
        )
    except Exception:  # noqa: BLE001 - the coverage stands; the notice says to copy the link
        logger.exception("driver link for assignment %s not sent", assignment.pk)
        result = sender.SendResult(sent=False, deliveries=[], errors=["unexpected error"])
    try:
        if not result.sent and not _skipped_by_choice(assignment):
            Notification.notify(
                trip.lead,
                Notification.Kind.SYNC_FAILED,
                title=f"Driver link to {assignment.provider_name} not sent"[:160],
                detail=f"Trip #{trip.reference}: copy the driver link from the drawer."[:255],
            )
    except Exception:  # noqa: BLE001 - a notice that cannot be written never fails the click
        logger.exception("notice for driver link %s not written", assignment.pk)
    return result


def link_send_state(assignment: Assignment) -> str:
    """What the drawer says about this coverage's link message: "sent", "not_sent",
    "no_address" (nowhere to send it), or "" (not confirmed coverage, nothing tried, or a
    send still in flight, which must not offer a Resend)."""
    from apps.messaging.models import TouchPoint

    if assignment.status != Assignment.Status.CONFIRMED:
        return ""
    latest = _latest_touchpoint(assignment)
    if latest is None:
        return "" if _has_address(assignment) else "no_address"
    status = latest[0]
    if status == TouchPoint.Status.SCHEDULED:
        return ""
    return "sent" if status == TouchPoint.Status.SENT else "not_sent"
