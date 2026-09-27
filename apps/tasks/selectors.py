"""Green-lit (APC-56) — derived, never stored.

A trip is green-lit when dispatch needs nothing more from anyone:

- it has tasks at all (an order booked before the task engine has none, and "nothing
  open" must not read as "all done"),
- every trip-level task is closed (done, skipped or not applicable),
- the order-level tasks in `ORDER_BLOCKERS` are closed — only the wedding answers; a
  non-wedding order simply has none,
- the trip has no unresolved `DispatchException`,
- and no open `TripIssue` (APC-59).

`_blocking_filters` is the single place to add a condition — Phase B adds "no open
TripReview issue" there, and both the annotation and the per-trip check pick it up.

Post-trip tasks (APC-58) never block: green-lit is about the trip before it runs, and the
review that follows it isn't something dispatch is waiting on.
"""

from __future__ import annotations

from django.db.models import (
    BooleanField,
    Exists,
    ExpressionWrapper,
    OuterRef,
    Prefetch,
    Q,
    prefetch_related_objects,
)

from apps.dispatch.models import DispatchException
from apps.reservations.models import Reservation, TripIssue

from .definitions import POST_TRIP_KINDS, REGISTRY
from .models import Task

ORDER_BLOCKERS = ("wedding_names", "day_of_contact")
_ORDER = {k.key: i for i, k in enumerate(REGISTRY)}


def _blocking_filters() -> list[Exists]:
    """Each is an Exists over the outer Reservation; any match means not green-lit."""
    return [
        Exists(
            Task.objects.filter(reservation=OuterRef("pk"), status__in=Task.UNRESOLVED).exclude(
                kind__in=POST_TRIP_KINDS
            )
        ),
        Exists(
            Task.objects.filter(
                lead=OuterRef("lead_id"),
                reservation__isnull=True,
                kind__in=ORDER_BLOCKERS,
                status__in=Task.UNRESOLVED,
            )
        ),
        Exists(
            DispatchException.objects.filter(reservation=OuterRef("pk"), resolved_at__isnull=True)
        ),
        Exists(_open_issues(OuterRef("pk"))),
    ]


def _open_issues(trip_ref):
    return TripIssue.objects.open().filter(review__reservation=trip_ref)


def with_green_lit(qs):
    """Annotate `green_lit` on a Reservation queryset — one expression, no extra query."""
    condition = Q(Exists(Task.objects.filter(reservation=OuterRef("pk"))))
    for blocker in _blocking_filters():
        condition &= ~Q(blocker)
    return qs.annotate(green_lit=ExpressionWrapper(condition, output_field=BooleanField()))


def green_lit(reservation: Reservation) -> bool:
    return (
        with_green_lit(Reservation.objects.filter(pk=reservation.pk))
        .values_list("green_lit", flat=True)
        .first()
        is True
    )


def task_prefetches() -> list[Prefetch]:
    """What `attach_green_lit` reads, for callers building their own queryset."""
    return [
        # Each task row carries whether its trip has an open TripIssue. A trip with no
        # tasks is never green-lit anyway, so this costs the board and drawer no query.
        Prefetch(
            "tasks",
            queryset=Task.objects.annotate(
                trip_has_open_issue=Exists(_open_issues(OuterRef("reservation_id")))
            ),
            to_attr="task_rows",
        ),
        Prefetch(
            "lead__tasks",
            queryset=Task.objects.filter(
                reservation__isnull=True, kind__in=ORDER_BLOCKERS, status__in=Task.UNRESOLVED
            ),
            to_attr="order_blocker_rows",
        ),
        Prefetch(
            "dispatch_exceptions",
            queryset=DispatchException.objects.filter(resolved_at__isnull=True),
            to_attr="open_exceptions",
        ),
    ]


def attach_green_lit(trips) -> list[Reservation]:
    """Set `green_lit` and `task_blockers` (labels of what's missing) on each trip.

    Only the `task_prefetches()` the caller hasn't already applied are fetched (the board
    brings its own `open_exceptions`) — at most three queries total, never per trip.
    """
    trips = list(trips)
    if trips:
        first = trips[0]
        owners = {"task_rows": first, "order_blocker_rows": first.lead, "open_exceptions": first}
        missing = [p for p in task_prefetches() if not hasattr(owners[p.to_attr], p.to_attr)]
        prefetch_related_objects(trips, *missing)
    for trip in trips:
        open_rows = sorted(
            (
                t
                for t in trip.task_rows
                if t.status in Task.UNRESOLVED and t.kind not in POST_TRIP_KINDS
            ),
            key=lambda t: _ORDER.get(t.kind, 99),
        )
        order_rows = sorted(trip.lead.order_blocker_rows, key=lambda t: _ORDER.get(t.kind, 99))
        blockers = [t.label for t in open_rows] + [t.label for t in order_rows]
        blockers += [e.get_kind_display() for e in trip.open_exceptions]
        if any(getattr(t, "trip_has_open_issue", False) for t in trip.task_rows):
            blockers.append("Open trip issue")
        trip.task_blockers = blockers
        trip.green_lit = bool(trip.task_rows) and not blockers
    return trips


def _in_registry_order(tasks) -> list[Task]:
    return sorted(tasks, key=lambda t: (_ORDER.get(t.kind, 99), t.pk))


def checklist_for_trip(reservation: Reservation) -> list[Task]:
    """What the drawer and the trip-line checklist show (APC-54): the trip's own tasks,
    then the order-level tasks that hold up its green-lit. One query."""
    qs = (
        Task.objects.filter(
            Q(reservation_id=reservation.pk)
            | Q(lead_id=reservation.lead_id, reservation__isnull=True, kind__in=ORDER_BLOCKERS)
        )
        .select_related("reservation", "completed_by", "assignee")
        .with_order_tz()
    )
    return _in_registry_order(qs)


def checklist_for_order(lead) -> list[Task]:
    """The order-level checklist on the workspace and order page. One query."""
    qs = (
        Task.objects.filter(lead_id=lead.pk, reservation__isnull=True)
        .with_order_tz()
        .select_related("completed_by", "assignee")
    )
    return _in_registry_order(qs)
