"""Every assignment state change goes through here — views never touch Assignment directly.

The "one active assignment per trip" rule is enforced in `_claim` rather than as a DB
constraint on purpose: it needs a conditional uniqueness (status IN offered/confirmed),
and MySQL — the local and test database — has no partial indexes, so a `condition=`
constraint would exist on prod Postgres and silently not exist where the tests run.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import transaction
from django.utils import timezone

from apps.notifications.email import send_html_email
from apps.notifications.models import Notification
from apps.reservations.models import Reservation
from apps.vendors.models import Vendor

from .models import Assignment


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
) -> Assignment:
    """Create an assignment, refusing if the trip already has an active one.

    Locks the reservation row so two dispatchers assigning the same trip at the same
    moment can't both pass the check.
    """
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
        "payout": assignment.payout,
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
    """Offer the trip to an affiliate and email them the trip sheet.

    Delivery is best-effort: a vendor with no email on file (or a send that fails) still
    gets the assignment recorded, because he may well be arranging it by phone in parallel.
    """
    assignment = _claim(
        reservation, vendor, payout=payout, note=note, status=Assignment.Status.OFFERED
    )
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
    """Record coverage already arranged out of band (phone, text) — no offer step."""
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
    """We pulled the offer, or unassigned confirmed coverage."""
    return _resolve(assignment, Assignment.Status.WITHDRAWN, note=note)
