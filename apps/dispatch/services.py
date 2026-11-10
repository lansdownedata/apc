"""Every assignment state change goes through here — views never touch Assignment directly.

The "one active assignment per trip" rule is enforced in `_claim` rather than as a DB
constraint on purpose: it needs a conditional uniqueness (status IN offered/confirmed),
and MySQL — the local and test database — has no partial indexes, so a `condition=`
constraint would exist on prod Postgres and silently not exist where the tests run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core import outbound
from apps.fleet.models import Driver, Vehicle
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.models import Reservation
from apps.vendors.models import Vendor

from . import gnet_sync
from .models import Assignment, DispatchAlertConfig

logger = logging.getLogger(__name__)


class AssignmentError(Exception):
    """An illegal transition, or a second active assignment on one trip."""


def active_assignment(reservation: Reservation) -> Assignment | None:
    """The offer in play or the confirmed coverage — at most one per trip."""
    return reservation.assignments.active().select_related("vendor", "driver", "vehicle").first()


def _claim(
    reservation: Reservation,
    *,
    vendor: Vendor | None = None,
    driver: Driver | None = None,
    vehicle: Vehicle | None = None,
    payout: Decimal = Decimal("0"),
    note: str,
    status: str,
    channel: str = Assignment.Channel.MANUAL,
) -> Assignment:
    """Create an assignment, refusing if the trip can't legally be covered.

    Exactly one provider: an affiliate `vendor` (farm-out) or an in-house `driver`, with an
    optional fleet `vehicle` only alongside a driver. The DB CHECK constraints back this,
    but the service refuses first so the dispatcher gets a sentence, not a 500.

    The trip guards live here rather than in the view so every door (`send_offer`,
    `assign_direct`, `assign_in_house`) is covered: farming out an unsold quote emails a
    real affiliate a trip sheet for a trip nobody bought, and a cancelled trip needs no
    coverage at all.

    Locks the reservation row so two dispatchers assigning the same trip at the same
    moment can't both pass the already-active check.

    `channel` defaults to MANUAL — `assign_direct` and `assign_in_house` always want that,
    and it's the safe default for any future caller. `send_offer` passes GNET explicitly
    for a GNet-capable vendor, so the assignment's channel is correct from creation.
    """
    if (vendor is None) == (driver is None):
        raise AssignmentError("Choose either an affiliate or an in-house driver.")
    if vehicle is not None and driver is None:
        raise AssignmentError("A vehicle can only be assigned together with a driver.")
    if driver is not None and driver.status != Driver.Status.ACTIVE:
        raise AssignmentError(f"Driver {driver.driver_number} is inactive.")
    if vehicle is not None and vehicle.status != Vehicle.Status.ACTIVE:
        raise AssignmentError(f"{vehicle.name} is inactive.")
    if reservation.lead.status != Lead.Status.BOOKED:
        raise AssignmentError(f"Trip #{reservation.pk} isn't on a booked order.")
    if reservation.is_cancelled:
        raise AssignmentError(f"Trip #{reservation.pk} is cancelled.")
    with transaction.atomic():
        Reservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.assignments.active().exists():
            raise AssignmentError(f"Trip #{reservation.pk} already has an active assignment.")
        resolved = timezone.now() if status == Assignment.Status.CONFIRMED else None
        assignment = Assignment(
            reservation=reservation,
            vendor=vendor,
            driver=driver,
            vehicle=vehicle,
            payout=payout,
            note=note,
            status=status,
            channel=channel,
            resolved_at=resolved,
        )
        # In-house coverage carries its driver from the start, so its info is on file now.
        assignment.stamp_driver_info(DispatchAlertConfig.load())
        assignment.save()
    if assignment.status == Assignment.Status.CONFIRMED:
        _on_assignment_confirmed(assignment)
    _sync_tasks(reservation)
    return assignment


def _sync_tasks(reservation: Reservation) -> None:
    """Coverage changed — let the trip's tasks close (or a system-closed one reopen)."""
    from apps.tasks import services as tasks

    tasks.ensure_tasks(reservation.lead)


_TS = Reservation.TripStatus
# Confirmed coverage shows the trip Assigned unless it has already got further (Moe,
# 2026-11-10): a trip already Dispatched or under way is never moved back.
_BEFORE_ASSIGNED = frozenset(
    {
        "",
        _TS.UNASSIGNED,
        _TS.FARM_OUT_UNASSIGNED,
        _TS.PENDING,
        _TS.OFFERED,
        _TS.OFFERED_TO_AFFILIATE,
    }
)


def _set_status(reservation: Reservation, status: str) -> None:
    """A status the app sets for itself, through the one seam every status change takes
    (its event, messages, GNet farm-in push), logged as System."""
    from apps.reservations.models import TripStatusEvent
    from apps.reservations.services import set_trip_status

    set_trip_status(reservation, status, source=TripStatusEvent.Source.SYSTEM)


def _reopen(reservation: Reservation) -> None:
    """Coverage was let go: the trip goes back to Unassigned, clearing Dispatched, On The
    Way, Arrived and the rest (Moe, 2026-11-10). A trip that is done or cancelled (No Show
    included) keeps its status, and one with no status yet stays blank. Unassigned queues
    no customer message and has no GNet farm-in code, so nothing leaves the building."""
    from apps.dispatch.selectors import CANCELLED_STATUSES

    reservation.refresh_from_db(fields=["trip_status"])
    current = reservation.trip_status
    if current in ("", _TS.UNASSIGNED, _TS.FARM_OUT_UNASSIGNED, _TS.DONE, *CANCELLED_STATUSES):
        return
    _set_status(reservation, _TS.UNASSIGNED)


def _on_assignment_confirmed(assignment: Assignment) -> None:
    """Show the trip Assigned, then queue the T-48h affiliate confirmation for a
    newly-confirmed farm-out (APC-20), and the driver message if the trip is already
    dispatched with details on file."""
    from apps.messaging import events

    reservation = assignment.reservation
    if reservation.trip_status in _BEFORE_ASSIGNED:
        _set_status(reservation, _TS.ASSIGNED)
    events.schedule_dates(reservation.lead, reservation)
    events.sync_waiting(reservation)


def _on_assignment_released(reservation: Reservation) -> None:
    """The affiliate no longer holds the trip — drop any pending T-48h confirmation."""
    from apps.messaging import events
    from apps.messaging.models import MessageTemplate

    events.cancel(
        reservation.lead,
        triggers=["pickup"],
        reservation=reservation,
        audience=MessageTemplate.Audience.AFFILIATE,
    )


def offer_email_context(assignment: Assignment) -> dict:
    """Trip-sheet fields for the affiliate. Deliberately omits the customer price —
    an affiliate sees only what we pay them."""
    trip = assignment.reservation
    return {
        "vendor": assignment.vendor,
        "trip": trip,
        # Pre-formatted here rather than in the template: the email templates have no
        # humanize filters, and `send_quote` formats its money the same way.
        "payout": f"{assignment.payout:,.2f}",
        "stops": list(trip.ordered_stops),
        "company_name": settings.COMPANY_NAME,
        "company_phone": settings.COMPANY_PHONE,
        "company_email": settings.COMPANY_EMAIL,
    }


def _affiliate_values(vendor: Vendor, trip: Reservation) -> dict[str, str]:
    """What the affiliate offer and cancellation rows fill from this click. A trip with no
    pickup date yet says so in the subject."""
    day = f"{trip.pickup_date:%b %-d}" if trip.pickup_date else "date to be set"
    return {"vendor_name": vendor.name, "trip_day": day}


def send_offer(
    reservation: Reservation, vendor: Vendor, *, payout: Decimal, note: str = ""
) -> Assignment:
    """Offer the trip to an affiliate — over GNet if it's a GNet-capable vendor, by
    trip-sheet email otherwise (the "Trip offer" message row).

    Both channels are best-effort: a GNet gateway problem or an email that fails to send
    (or a vendor with no email on file) still leaves the assignment recorded, because
    dispatch may well be arranging coverage by phone in parallel. A dispatcher's action
    must never fail because a network — ours or the gateway's — is down.

    `gnet_sync.push_assignment` is trusted to make good on that guarantee itself: every
    gateway-side failure it can encounter (a non-2xx response, a `requests`-level
    transport error, an unmapped vehicle/vendor) is caught inside `gnet_sync`/`gnet.py`
    and turned into a terminal `GnetEvent` plus an alert, never a raised exception — see
    `apps.integrations.gnet._request`. This call is therefore made directly, with no
    defensive `try/except` here: anything that still escapes it is a genuine bug, not a
    network hiccup, and should fail loudly rather than be swallowed.
    """
    channel = Assignment.Channel.GNET if vendor.is_gnet_capable else Assignment.Channel.MANUAL
    if channel == Assignment.Channel.MANUAL and vendor.email and not outbound.enabled():
        # An emailed offer the affiliate never receives is a trip that looks covered and
        # is not. Refuse before the claim; coverage arranged by phone goes through Assign.
        raise AssignmentError(
            f"{outbound.OFF_MESSAGE} Use Assign to record coverage arranged by phone."
        )
    assignment = _claim(
        reservation,
        vendor=vendor,
        payout=payout,
        note=note,
        status=Assignment.Status.OFFERED,
        channel=channel,
    )

    if channel == Assignment.Channel.GNET:
        gnet_sync.push_assignment(assignment)
        return assignment

    if not vendor.email:
        return assignment

    from apps.messaging import rendering, sender

    try:
        sheet = offer_email_context(assignment)
        sent = sender.send_now(
            "affiliate_offer",
            reservation.lead,
            reservation,
            assignment=assignment,
            extra_ctx={
                **_affiliate_values(vendor, reservation),
                "payout": f"${assignment.payout:,.2f}",
            },
            blocks={"trip_sheet": rendering.block("trip_sheet", sheet)},
            logo=True,
            preheader=rendering.preheader("vendor_offer", sheet),
        ).sent
    except Exception:  # noqa: BLE001 — the offer is recorded; the alert says to phone
        logger.exception("offer email for assignment %s failed", assignment.pk)
        sent = False
    if sent:
        return assignment

    Notification.notify(
        reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=f"Offer email to {vendor.name} failed"[:160],
        detail=f"Trip #{reservation.pk} is still marked offered — follow up by phone."[:255],
    )
    return assignment


def send_cancellation(assignment: Assignment) -> bool:
    """Tell a farmed-out affiliate the trip is off (the "Trip cancelled" message row). The
    offer email's counterpart.

    Not automatic, and deliberately so: some reassignments are the affiliate's own doing
    and a "we've released it" note reads oddly back at them. The dispatcher decides.

    In-house has nobody to tell, and a GNet assignment is released over the gateway by
    `withdraw` — a second message there would say what the network already said.

    Best effort, like `send_offer`: coverage has already changed, so a mail problem must
    never read as a failed reassignment. Returns whether anything went out.
    """
    vendor = assignment.vendor
    if assignment.is_in_house or vendor is None or not vendor.email:
        return False
    from apps.messaging import rendering, sender

    trip = assignment.reservation
    try:
        # No payout, no total, no money of any kind — see the sheet.
        sheet = rendering.block(
            "cancelled_trip_sheet", {"trip": trip, "stops": list(trip.ordered_stops)}
        )
        return sender.send_now(
            "affiliate_cancel",
            trip.lead,
            trip,
            assignment=assignment,
            extra_ctx=_affiliate_values(vendor, trip),
            blocks={"cancelled_trip_sheet": sheet},
            preheader=rendering.preheader("vendor_cancel", {}),
        ).sent
    except Exception:  # noqa: BLE001 — best effort, like the offer
        logger.exception("cancellation email for assignment %s failed", assignment.pk)
        return False


def assign_direct(
    reservation: Reservation, vendor: Vendor, *, payout: Decimal, note: str = ""
) -> Assignment:
    """Record coverage already arranged out of band (phone, text) — no offer step.

    Always MANUAL, even for a GNet-capable vendor: this is a phone-arranged coverage
    record, not a farm-out, so it never touches the gateway. The affiliate has accepted
    (by phone), so it is the affiliate-accepted message event too; in-house coverage goes
    through `assign_in_house` and is not.
    """
    from apps.messaging import events

    assignment = _claim(
        reservation, vendor=vendor, payout=payout, note=note, status=Assignment.Status.CONFIRMED
    )
    events.emit("affiliate_accepted", reservation.lead, reservation)
    return assignment


def assign_in_house(
    reservation: Reservation, driver: Driver, *, vehicle: Vehicle | None = None, note: str = ""
) -> Assignment:
    """Cover the trip with one of our own drivers (and optionally a fleet unit).

    Straight to CONFIRMED: there is no offer step for your own driver, no payout, and
    nothing leaves the building — the trip sheet to the driver is a follow-on feature.
    """
    return _claim(
        reservation, driver=driver, vehicle=vehicle, note=note, status=Assignment.Status.CONFIRMED
    )


def _resolve(
    assignment: Assignment, status: str, *, note: str = "", reopen: bool = True
) -> Assignment:
    """Settle an active assignment. Declined or withdrawn coverage puts the trip back to
    Unassigned (`_reopen`) unless ``reopen`` is False."""
    if not assignment.is_active:
        raise AssignmentError(
            f"Assignment {assignment.pk} is {assignment.get_status_display().lower()}."
        )
    was_confirmed = assignment.status == Assignment.Status.CONFIRMED
    assignment.status = status
    assignment.resolved_at = timezone.now()
    fields = ["status", "resolved_at", "updated_at"]
    if note:
        assignment.note = note
        fields.append("note")
    assignment.save(update_fields=fields)
    if status == Assignment.Status.CONFIRMED:
        _on_assignment_confirmed(assignment)
    else:
        if was_confirmed and not assignment.is_in_house:
            _on_assignment_released(assignment.reservation)
        if reopen:
            _reopen(assignment.reservation)
    _sync_tasks(assignment.reservation)
    return assignment


_IN_HOUSE_ONLY_UNASSIGN = "An in-house assignment can only be unassigned."


def confirm(assignment: Assignment) -> Assignment:
    """The affiliate accepted."""
    from apps.messaging import events

    if assignment.is_in_house:
        raise AssignmentError(_IN_HOUSE_ONLY_UNASSIGN)
    if assignment.status == Assignment.Status.CONFIRMED:
        return assignment
    confirmed = _resolve(assignment, Assignment.Status.CONFIRMED)
    reservation = confirmed.reservation
    events.emit("affiliate_accepted", reservation.lead, reservation)
    return confirmed


def decline(assignment: Assignment, *, note: str = "") -> Assignment:
    """The affiliate said no — the trip goes back to uncovered (and Unassigned)."""
    if assignment.is_in_house:
        raise AssignmentError(_IN_HOUSE_ONLY_UNASSIGN)
    return _resolve(assignment, Assignment.Status.DECLINED, note=note)


def withdraw(
    assignment: Assignment, *, note: str = "", release_gateway: bool = True, reopen: bool = True
) -> Assignment:
    """We pulled the offer, or unassigned confirmed coverage. The trip goes back to
    Unassigned (`_reopen`); `reopen=False` is for `release_trips`, whose trips stop needing
    coverage altogether (cancelled, or about to be deleted).

    `release_gateway=False` is for the one caller whose trip is ALREADY released on the
    partner's side: an affiliate-initiated `CANCEL` callback. Echoing a DELETE back
    there earns a rejection and a spurious "GNet cancel failed" alert alongside the
    correct one, on every affiliate cancellation — and spends a 10s-timeout outbound
    call inside the gateway's 15s callback budget. Every other caller keeps the default.

    For a GNet-channel assignment this also releases the affiliate on the gateway. The
    state change above has already committed, so a gateway problem must never look like
    a failed withdraw to the dispatcher. As in `send_offer`, that guarantee lives inside
    `gnet_sync.cancel_assignment` itself (every gateway-side failure becomes a terminal
    `GnetEvent` plus an alert, never a raised exception), so this calls it directly with
    no defensive `try/except` — anything that still escapes is a genuine bug and should
    be loud.

    Routes off `channel`, never off `bool(assignment.gnet_transaction_id)` — a cancelled
    assignment deliberately keeps its transaction id (append-only history), and
    `cancel_assignment` itself is what guards against sending a second cancel.
    """
    resolved = _resolve(assignment, Assignment.Status.WITHDRAWN, note=note, reopen=reopen)
    if release_gateway and resolved.channel == Assignment.Channel.GNET:
        gnet_sync.cancel_assignment(resolved)
    return resolved


DRIVER_FIELD_LABELS = {
    "name": "driver's name",
    "cell": "driver's cell",
    "vehicle_desc": "vehicle description",
    "vehicle_number": "vehicle number",
}


def missing_driver_fields(assignment: Assignment, config: DispatchAlertConfig) -> list[str]:
    """Labels of the required driver details still blank, in a stable order."""
    info = assignment.driver_info or {}
    return [
        DRIVER_FIELD_LABELS[key]
        for key in config.required_driver_fields()
        if not (info.get(key) or "").strip()
    ]


def driver_details_ready(assignment: Assignment, config: DispatchAlertConfig) -> bool:
    """Every required driver detail is on file — the one check all consumers share."""
    return not missing_driver_fields(assignment, config)


def set_driver_info(
    assignment: Assignment, *, name: str, cell: str, vehicle_desc: str, vehicle_number: str
) -> Assignment:
    """Record a farmed-out trip's driver + vehicle detail, and release it to the customer
    the moment it's complete (APC-21).

    In-house coverage already carries this via its `Driver` + `Vehicle` — there's nothing
    free-entry to set, so this refuses rather than silently no-opping fields nobody reads.
    """
    if assignment.is_in_house:
        raise AssignmentError("In-house coverage already has driver and vehicle on file.")
    if assignment.status != Assignment.Status.CONFIRMED:
        raise AssignmentError("Only confirmed coverage can carry driver info.")
    assignment.driver_name = name
    assignment.driver_cell = cell
    assignment.vehicle_desc = vehicle_desc
    assignment.vehicle_number = vehicle_number
    fields = ["driver_name", "driver_cell", "vehicle_desc", "vehicle_number", "updated_at"]
    config = DispatchAlertConfig.load()
    if assignment.stamp_driver_info(config):
        fields.append("driver_info_at")
    assignment.save(update_fields=fields)
    from apps.messaging import events

    events.sync_waiting(assignment.reservation, dispatch_config=config)
    _sync_tasks(assignment.reservation)
    return assignment


def release_trips(reservations: Iterable[Reservation], *, note: str) -> list[Assignment]:
    """Withdraw whatever active assignment each of `reservations` still has.

    Called when trips stop needing coverage — a cancelled order, a deleted trip. Their
    tasks go not-applicable here too (APC-51), so coverage and task work stay in step. The board
    excludes both, and no screen lists assignments by vendor, so an assignment left active
    is one no dispatcher can reach while the affiliate is still holding a trip that no
    longer exists. One query for the whole set rather than a lookup per trip.

    Isolated per assignment: a gateway problem can no longer raise out of `withdraw` at
    all (see its docstring), but a genuine bug or a DB hiccup mid-batch still could, and
    a list comprehension would let that abort the whole call — stranding every later
    trip in the batch in exactly the "affiliate still holds a trip that no longer
    exists" state this function exists to prevent. One bad row is logged loudly and
    skipped; the rest of the batch still releases, and the return value is only the
    assignments that actually did.
    """
    reservations = list(reservations)
    from apps.tasks import services as tasks

    tasks.cancel_for_trips(reservations)
    released = []
    for assignment in Assignment.objects.active().filter(reservation__in=reservations):
        try:
            released.append(withdraw(assignment, note=note, reopen=False))
        except Exception:  # noqa: BLE001 - one bad row must not strand the rest of the batch
            logger.exception("Failed to release assignment %s", assignment.pk)
    return released


def record_insurance_override(assignment: Assignment, user) -> bool:
    """Stamp who offered or confirmed this trip to an affiliate not insured for it
    (APC-72). Warn, don't block: the assignment stands either way. Returns whether it
    stamped; a covered affiliate, or in-house coverage, leaves nothing to record."""
    from apps.vendors.models import Vendor

    from . import selectors

    if assignment.is_in_house:
        return False
    vendor = Vendor.objects.prefetch_related("policies").get(pk=assignment.vendor_id)
    status = selectors.insurance_for(vendor, assignment.reservation)["status"]
    if status == "covered":
        return False
    assignment.insurance_override = status
    assignment.insurance_override_by = user
    assignment.insurance_override_at = timezone.now()
    assignment.save(
        update_fields=[
            "insurance_override",
            "insurance_override_by",
            "insurance_override_at",
            "updated_at",
        ]
    )
    return True
