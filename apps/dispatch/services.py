"""Every assignment state change goes through here — views never touch Assignment directly.

The "one active assignment per trip" rule is enforced in `_claim` rather than as a DB
constraint on purpose: it needs a conditional uniqueness (status IN offered/confirmed),
and MySQL — the local and test database — has no partial indexes, so a `condition=`
constraint would exist on prod Postgres and silently not exist where the tests run.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

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


def send_offer(
    reservation: Reservation, vendor: Vendor, *, payout: Decimal, note: str = ""
) -> Assignment:
    """Offer the trip to an affiliate and wait for their answer."""
    return _claim(reservation, vendor, payout=payout, note=note, status=Assignment.Status.OFFERED)


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
