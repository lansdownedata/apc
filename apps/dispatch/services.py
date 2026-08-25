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
from django.contrib.staticfiles import finders
from django.db import transaction
from django.utils import timezone

from apps.leads.models import Lead
from apps.notifications.email import send_html_email
from apps.notifications.models import Notification
from apps.reservations.models import Reservation
from apps.vendors.models import Vendor

from . import gnet_sync
from .models import Assignment

logger = logging.getLogger(__name__)


class AssignmentError(Exception):
    """An illegal transition, or a second active assignment on one trip."""


def active_assignment(reservation: Reservation) -> Assignment | None:
    """The offer in play or the confirmed coverage — at most one per trip."""
    return reservation.assignments.active().select_related("vendor").first()


def _claim(
    reservation: Reservation,
    vendor: Vendor,
    *,
    payout: Decimal,
    note: str,
    status: str,
    channel: str = Assignment.Channel.MANUAL,
) -> Assignment:
    """Create an assignment, refusing if the trip can't legally be farmed out.

    The trip guards live here rather than in the view so both doors (`send_offer` and
    `assign_direct`) are covered: farming out an unsold quote emails a real affiliate a
    trip sheet for a trip nobody bought, and a cancelled trip needs no coverage at all.

    Locks the reservation row so two dispatchers assigning the same trip at the same
    moment can't both pass the already-active check.

    `channel` defaults to MANUAL — `assign_direct` always wants that, and it's the safe
    default for any future caller. `send_offer` passes GNET explicitly for a GNet-capable
    vendor, so the assignment's channel is correct from creation rather than patched on
    afterwards.
    """
    if reservation.lead.status != Lead.Status.BOOKED:
        raise AssignmentError(f"Trip #{reservation.pk} isn't on a booked order.")
    if reservation.is_cancelled:
        raise AssignmentError(f"Trip #{reservation.pk} is cancelled.")
    with transaction.atomic():
        Reservation.objects.select_for_update().get(pk=reservation.pk)
        if reservation.assignments.active().exists():
            raise AssignmentError(f"Trip #{reservation.pk} already has an active assignment.")
        resolved = timezone.now() if status == Assignment.Status.CONFIRMED else None
        return Assignment.objects.create(
            reservation=reservation,
            vendor=vendor,
            payout=payout,
            note=note,
            status=status,
            channel=channel,
            resolved_at=resolved,
        )


def _offer_logo() -> str | None:
    """Absolute path to the email banner logo PNG, or None if it isn't collectable."""
    return finders.find("brand/apc-logo-email.png")


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
        # The banner logo is embedded as an inline CID attachment (see _offer_logo /
        # the send_html_email call) so it renders without a remote fetch.
        "logo_cid": "logo" if _offer_logo() else "",
    }


def send_offer(
    reservation: Reservation, vendor: Vendor, *, payout: Decimal, note: str = ""
) -> Assignment:
    """Offer the trip to an affiliate — over GNet if it's a GNet-capable vendor, by
    trip-sheet email otherwise.

    Both channels are best-effort: a GNet gateway problem or an email that fails to send
    (or a vendor with no email on file) still leaves the assignment recorded, because
    dispatch may well be arranging coverage by phone in parallel. A dispatcher's action
    must never fail because a network — ours or the gateway's — is down.
    """
    channel = Assignment.Channel.GNET if vendor.is_gnet_capable else Assignment.Channel.MANUAL
    assignment = _claim(
        reservation,
        vendor,
        payout=payout,
        note=note,
        status=Assignment.Status.OFFERED,
        channel=channel,
    )

    if channel == Assignment.Channel.GNET:
        try:
            gnet_sync.push_assignment(assignment)
        except Exception:  # noqa: BLE001 — best-effort; gnet_sync already alerts on failure
            logger.exception("GNet push failed for assignment %s", assignment.pk)
        return assignment

    if not vendor.email:
        return assignment

    if reservation.pickup_date:
        subject = f"Trip offer — {reservation.pickup_date:%b %-d}"
    else:
        subject = "Trip offer"
    logo = _offer_logo()
    if send_html_email(
        to=vendor.email,
        subject=subject,
        template="vendor_offer",
        context=offer_email_context(assignment),
        inline_images={"logo": logo} if logo else None,
    ):
        return assignment

    Notification.notify(
        reservation.lead,
        Notification.Kind.SYNC_FAILED,
        title=f"Offer email to {vendor.name} failed"[:160],
        detail=f"Trip #{reservation.pk} is still marked offered — follow up by phone."[:255],
    )
    return assignment


def assign_direct(
    reservation: Reservation, vendor: Vendor, *, payout: Decimal, note: str = ""
) -> Assignment:
    """Record coverage already arranged out of band (phone, text) — no offer step.

    Always MANUAL, even for a GNet-capable vendor: this is a phone-arranged coverage
    record, not a farm-out, so it never touches the gateway.
    """
    return _claim(reservation, vendor, payout=payout, note=note, status=Assignment.Status.CONFIRMED)


def _resolve(assignment: Assignment, status: str, *, note: str = "") -> Assignment:
    if not assignment.is_active:
        raise AssignmentError(
            f"Assignment {assignment.pk} is {assignment.get_status_display().lower()}."
        )
    assignment.status = status
    assignment.resolved_at = timezone.now()
    fields = ["status", "resolved_at", "updated_at"]
    if note:
        assignment.note = note
        fields.append("note")
    assignment.save(update_fields=fields)
    return assignment


def confirm(assignment: Assignment) -> Assignment:
    """The affiliate accepted."""
    if assignment.status == Assignment.Status.CONFIRMED:
        return assignment
    return _resolve(assignment, Assignment.Status.CONFIRMED)


def decline(assignment: Assignment, *, note: str = "") -> Assignment:
    """The affiliate said no — the trip goes back to uncovered."""
    return _resolve(assignment, Assignment.Status.DECLINED, note=note)


def withdraw(assignment: Assignment, *, note: str = "") -> Assignment:
    """We pulled the offer, or unassigned confirmed coverage.

    For a GNet-channel assignment this also releases the affiliate on the gateway. That
    call is best-effort, same as `send_offer`'s: the state change here has already
    committed, so a gateway problem must never look like a failed withdraw to the
    dispatcher — it only ever raises an alert (see `gnet_sync.cancel_assignment`).

    Routes off `channel`, never off `bool(assignment.gnet_transaction_id)` — a cancelled
    assignment deliberately keeps its transaction id (append-only history), and
    `cancel_assignment` itself is what guards against sending a second cancel.
    """
    resolved = _resolve(assignment, Assignment.Status.WITHDRAWN, note=note)
    if resolved.channel == Assignment.Channel.GNET:
        try:
            gnet_sync.cancel_assignment(resolved)
        except Exception:  # noqa: BLE001 — best-effort; gnet_sync already alerts on failure
            logger.exception("GNet cancel failed for assignment %s", resolved.pk)
    return resolved


def release_trips(reservations: Iterable[Reservation], *, note: str) -> list[Assignment]:
    """Withdraw whatever active assignment each of `reservations` still has.

    Called when trips stop needing coverage — a cancelled order, a deleted trip. The board
    excludes both, and no screen lists assignments by vendor, so an assignment left active
    is one no dispatcher can reach while the affiliate is still holding a trip that no
    longer exists. One query for the whole set rather than a lookup per trip.
    """
    return [
        withdraw(assignment, note=note)
        for assignment in Assignment.objects.active().filter(reservation__in=reservations)
    ]
