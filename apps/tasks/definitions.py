"""The task-kind registry (APC-50) — definitions in code, task rows in the DB.

The same split as the touch-point templates and the cron registry: what a kind *is*
(department, level, when it opens and falls due, how it closes itself) lives here; the
rows in `Task` are just instances of these.

⚠ PLACEHOLDER OFFSETS. Every `opens` / `due` below is a working guess, not the client's
number. He has said he'll define task-engine requirements in detail after APC-27; confirm
each offset with him before merge (APC-50 open question 1). They're in one table on
purpose so that confirmation is a one-file change.

Predicates read a `LeadFacts` snapshot (see `facts.py`) rather than the ORM, so evaluating
every task on a 10-trip order — or every open task in the cron — costs a fixed number of
queries, not one per task.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from django.db import models

from apps.accounts.models import Department

if TYPE_CHECKING:
    from .facts import LeadFacts
    from .models import Task


class Level(models.TextChoices):
    ORDER = "order", "Order"
    TRIP = "trip", "Trip"


@dataclass(frozen=True)
class BeforePickup:
    """An instant anchored on pickup, counted back in the trip's own timezone.

    `day_start=True` means midnight at the start of the local day `days` before pickup —
    the "day before" boundary the repo rule says must follow the trip's zone, never UTC or
    the server's. Otherwise it's the exact instant `days`/`hours` before pickup.
    """

    days: int = 0
    hours: int = 0
    day_start: bool = False

    def resolve(self, pickup_at: datetime | None) -> datetime | None:
        if pickup_at is None:
            return None
        if self.day_start:
            day = pickup_at.date() - timedelta(days=self.days)
            local = datetime.combine(day, time(0, 0), tzinfo=pickup_at.tzinfo)
            return (local - timedelta(hours=self.hours)).astimezone(UTC)
        return (pickup_at - timedelta(days=self.days, hours=self.hours)).astimezone(UTC)


@dataclass(frozen=True)
class AfterOpen:
    """Due a fixed time after the task opened — for work that isn't pickup-anchored."""

    hours: int = 0

    def resolve_from(self, opened_at: datetime) -> datetime:
        return opened_at + timedelta(hours=self.hours)


Predicate = Callable[["Task", "LeadFacts"], bool]
Applies = Callable[["LeadFacts", object], bool]


def _always(facts: LeadFacts, reservation: object) -> bool:
    return True


def _wedding_only(facts: LeadFacts, reservation: object) -> bool:
    return facts.is_wedding


def _entered_post_trip(facts: LeadFacts, reservation) -> bool:
    return reservation.pk in facts.post_trip_ids


def _farmed_out(facts: LeadFacts, reservation) -> bool:
    """An affiliate covered the trip, so there's an affiliate to pay. In-house coverage
    (our own driver) and a trip nobody covered have no payable."""
    a = facts.active_assignment(reservation.pk)
    return a is not None and not a.is_in_house


@dataclass(frozen=True)
class TaskKind:
    key: str
    label: str
    department: str
    level: str
    due: BeforePickup | AfterOpen
    # None = opens the moment the task is generated (the booking event).
    opens: BeforePickup | None = None
    applies: Applies = _always
    auto_complete: Predicate | None = None
    # Closes an unresolved task as not-applicable (by the system) when true — work the
    # data says will never be needed, e.g. invoicing overtime nobody approved.
    not_applicable: Predicate | None = None
    # A4: False for checkpoints the dispatch monitor already alerts on (decision D2).
    escalates: bool = True
    # APC-58 post-trip workflow. A post-trip kind with no `opens_after` is the stage-1 entry
    # (created when the trip enters post-trip); the rest chain through `opens_after`.
    post_trip: bool = False
    # Stage chaining: this kind is created once every existing task of these kinds on the
    # same trip is closed (at least one must exist). A kind whose predecessors don't apply
    # to a trip — the payables on an in-house trip — simply isn't waited on.
    opens_after: tuple[str, ...] = ()


# --- predicates ----------------------------------------------------------------------


def _deposit_paid(task: Task, facts: LeadFacts) -> bool:
    plan = facts.plan
    return bool(plan and plan.deposit_status == plan.DepositStatus.PAID)


def _balance_paid(task: Task, facts: LeadFacts) -> bool:
    plan = facts.plan
    return bool(plan and plan.balance_status == plan.BalanceStatus.PAID)


def _terms_accepted(task: Task, facts: LeadFacts) -> bool:
    return facts.lead.accepted_terms_at is not None


def _wedding_names(task: Task, facts: LeadFacts) -> bool:
    return bool(facts.lead.wedding_name.strip())


def _day_of_contact(task: Task, facts: LeadFacts) -> bool:
    lead = facts.lead
    return bool(lead.day_of_contact_name.strip() and lead.day_of_contact_phone.strip())


def _affiliate_assigned(task: Task, facts: LeadFacts) -> bool:
    return facts.active_assignment(task.reservation_id) is not None


def _affiliate_confirmed(task: Task, facts: LeadFacts) -> bool:
    a = facts.active_assignment(task.reservation_id)
    if a is None:
        return False
    if a.is_in_house:
        return a.status == a.Status.CONFIRMED
    return a.affiliate_confirmed_at is not None


def _driver_assigned(task: Task, facts: LeadFacts) -> bool:
    a = facts.active_assignment(task.reservation_id)
    if a is None:
        return False
    return a.driver_id is not None if a.is_in_house else bool(a.driver_name.strip())


def _driver_info_received(task: Task, facts: LeadFacts) -> bool:
    a = facts.active_assignment(task.reservation_id)
    return bool(a and a.has_driver_info)


def _driver_released(task: Task, facts: LeadFacts) -> bool:
    return task.reservation_id in facts.released_trip_ids


def _no_overtime_to_bill(task: Task, facts: LeadFacts) -> bool:
    """The completed review approved no billable minutes, or waived them (APC-59)."""
    review = facts.reviews.get(task.reservation_id)
    return bool(review and review.nothing_to_bill)


# --- the registry --------------------------------------------------------------------

_D = Department

REGISTRY: tuple[TaskKind, ...] = (
    TaskKind(
        key="deposit_received",
        label="Deposit received",
        department=_D.CUSTOMER_SERVICE,
        level=Level.ORDER,
        due=AfterOpen(hours=72),  # PLACEHOLDER
        auto_complete=_deposit_paid,
    ),
    TaskKind(
        # APC-55. Staff-booked orders never see the pay page, so nobody accepts anything —
        # staff check it off by hand until the client decides how those should close.
        key="contract_signed",
        label="Contract signed",
        department=_D.CUSTOMER_SERVICE,
        level=Level.ORDER,
        due=AfterOpen(hours=72),  # PLACEHOLDER
        auto_complete=_terms_accepted,
    ),
    TaskKind(
        key="wedding_names",
        label="Wedding names collected",
        department=_D.CUSTOMER_SERVICE,
        level=Level.ORDER,
        due=BeforePickup(days=7, day_start=True),  # PLACEHOLDER
        applies=_wedding_only,
        auto_complete=_wedding_names,
    ),
    TaskKind(
        key="day_of_contact",
        label="Day-of contact collected",
        department=_D.CUSTOMER_SERVICE,
        level=Level.ORDER,
        due=BeforePickup(days=7, day_start=True),  # PLACEHOLDER
        applies=_wedding_only,
        auto_complete=_day_of_contact,
    ),
    TaskKind(
        key="final_itinerary",
        label="Final itinerary received",
        department=_D.CUSTOMER_SERVICE,
        level=Level.ORDER,
        due=BeforePickup(days=7, day_start=True),  # PLACEHOLDER
    ),
    TaskKind(
        key="affiliate_assigned",
        label="Affiliate assigned",
        department=_D.AFFILIATE_MGMT,
        level=Level.TRIP,
        due=BeforePickup(days=3),  # PLACEHOLDER
        auto_complete=_affiliate_assigned,
        escalates=False,
    ),
    TaskKind(
        key="affiliate_confirmed",
        label="Affiliate confirmed",
        department=_D.AFFILIATE_MGMT,
        level=Level.TRIP,
        due=BeforePickup(days=2),  # PLACEHOLDER
        auto_complete=_affiliate_confirmed,
        escalates=False,
    ),
    TaskKind(
        key="driver_assigned",
        label="Driver assigned",
        department=_D.AFFILIATE_MGMT,
        level=Level.TRIP,
        due=BeforePickup(days=1),  # PLACEHOLDER
        auto_complete=_driver_assigned,
    ),
    TaskKind(
        key="driver_info_received",
        label="Driver info received",
        department=_D.AFFILIATE_MGMT,
        level=Level.TRIP,
        due=BeforePickup(days=1),  # PLACEHOLDER
        auto_complete=_driver_info_received,
        escalates=False,
    ),
    TaskKind(
        key="driver_released",
        label="Driver info released to client",
        department=_D.CUSTOMER_SERVICE,
        level=Level.TRIP,
        due=BeforePickup(hours=12),  # PLACEHOLDER
        auto_complete=_driver_released,
    ),
    TaskKind(
        # APC-56 — the one checkpoint that can't be read off data: dispatch confirms the
        # trip needs no further customer follow-up. Feeds green-lit with everything else.
        key="details_finalized",
        label="Details finalized",
        department=_D.OPERATIONS,
        level=Level.TRIP,
        due=BeforePickup(days=2),  # PLACEHOLDER
    ),
    TaskKind(
        key="final_balance_paid",
        label="Final balance paid",
        department=_D.ACCOUNTING,
        level=Level.ORDER,
        # Opens when the balance cron would charge it (T-30d) — PLACEHOLDER due.
        opens=BeforePickup(days=30, day_start=True),
        due=BeforePickup(days=28, day_start=True),
        auto_complete=_balance_paid,
    ),
    # --- post-trip (APC-58): ops review → accounting → customer service -------------
    TaskKind(
        # Stage 1. Closed by completing the trip review form (APC-59).
        key="ops_review",
        label="Operations review",
        department=_D.OPERATIONS,
        level=Level.TRIP,
        due=AfterOpen(hours=24),  # PLACEHOLDER
        applies=_entered_post_trip,
        post_trip=True,
    ),
    TaskKind(
        # Stage 2. Closed by the overtime charge (APC-60); not applicable when the review
        # approved nothing to bill.
        key="overtime_invoiced",
        label="Overtime invoiced",
        department=_D.ACCOUNTING,
        level=Level.TRIP,
        due=AfterOpen(hours=72),  # PLACEHOLDER
        not_applicable=_no_overtime_to_bill,
        post_trip=True,
        opens_after=("ops_review",),
    ),
    TaskKind(
        # Stage 2. Closed by approving the affiliate payable (APC-61).
        key="affiliate_payable_approved",
        label="Affiliate payable approved",
        department=_D.ACCOUNTING,
        level=Level.TRIP,
        due=AfterOpen(hours=72),  # PLACEHOLDER
        applies=_farmed_out,
        post_trip=True,
        opens_after=("ops_review",),
    ),
    TaskKind(
        # Stage 2, after the approval — paying an unapproved invoice isn't a step.
        key="affiliate_paid",
        label="Affiliate paid",
        department=_D.ACCOUNTING,
        level=Level.TRIP,
        due=AfterOpen(hours=24 * 14),  # PLACEHOLDER
        applies=_farmed_out,
        post_trip=True,
        opens_after=("affiliate_payable_approved",),
    ),
    TaskKind(
        # Stage 3. Closed by the thank-you touch-point (APC-62).
        key="thank_you_sent",
        label="Thank-you sent",
        department=_D.CUSTOMER_SERVICE,
        level=Level.TRIP,
        due=AfterOpen(hours=48),  # PLACEHOLDER
        post_trip=True,
        opens_after=("overtime_invoiced", "affiliate_payable_approved", "affiliate_paid"),
    ),
)

KINDS: dict[str, TaskKind] = {k.key: k for k in REGISTRY}

KIND_CHOICES = [(k.key, k.label) for k in REGISTRY]

POST_TRIP_KINDS = frozenset(k.key for k in REGISTRY if k.post_trip)

# Kinds something waits on — closing one of these may open the next stage.
PREDECESSOR_KINDS = frozenset(p for k in REGISTRY for p in k.opens_after)
