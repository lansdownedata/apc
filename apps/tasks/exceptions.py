"""The exception dashboard's numbers (APC-69): "what's wrong right now?"

Saved views over data that already exists, not new reports. `counts()` answers every view
in a fixed handful of aggregate queries; `snapshot()` records them once a business day so
the dashboard can show a 30-day trend (`trend()`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.accounts.models import Department
from apps.dispatch.models import DispatchException
from apps.payments.models import AffiliatePayable
from apps.payments.reports import unpaid_deposit_leads
from apps.reservations.models import TripIssue

from .models import ExceptionSnapshot, Task, TaskConfig

TREND_DAYS = 30


@dataclass(frozen=True)
class View:
    key: str
    label: str
    group: str


_DISPATCH_KINDS = {
    DispatchException.Kind.UNASSIGNED: "no_coverage",
    DispatchException.Kind.AFFILIATE_UNACKED: "affiliate_unconfirmed",
    DispatchException.Kind.NO_DRIVER_INFO: "no_driver_info",
}
# Open tasks of these kinds, whatever their due date.
_TASK_KINDS = {
    "day_of_contact": "missing_day_of_contact",
    "wedding_names": "missing_wedding_names",
    "ops_review": "overtime_awaiting",
    "overtime_invoiced": "overtime_awaiting",
}

VIEWS: tuple[View, ...] = (
    View("no_coverage", "No coverage", "Dispatch"),
    View("affiliate_unconfirmed", "Affiliate not confirmed", "Dispatch"),
    View("no_driver_info", "Driver info missing", "Dispatch"),
    View("missing_day_of_contact", "Day-of contact missing", "Customer details"),
    View("missing_wedding_names", "Wedding names missing", "Customer details"),
    View("open_issues", "Open trip issues", "Customer details"),
    View("unpaid_deposits", "Unpaid deposits", "Money"),
    View("unpaid_balances", "Unpaid balances", "Money"),
    View("overtime_awaiting", "Overtime awaiting decision", "Money"),
    View("missing_invoices", "Affiliate invoices missing", "Money"),
    View("pending_payments", "Affiliate payments pending", "Money"),
    *(View(f"overdue_{value}", label, "Overdue tasks") for value, label in Department.choices),
)


def counts(now=None) -> dict[str, int]:
    """Every view's open count. Five queries, whatever the volume."""
    now = now or timezone.now()
    out = {view.key: 0 for view in VIEWS}

    open_exceptions = (
        DispatchException.objects.filter(resolved_at__isnull=True, kind__in=_DISPATCH_KINDS)
        .values_list("kind")
        .annotate(n=Count("pk"))
    )
    for kind, n in open_exceptions:
        out[_DISPATCH_KINDS[kind]] = n

    out["unpaid_deposits"] = unpaid_deposit_leads(timezone.localdate(now)).count()

    open_tasks = (
        Task.objects.filter(status=Task.Status.OPEN)
        .values_list("kind", "department")
        .annotate(n=Count("pk"), overdue=Count("pk", filter=Q(due_at__lt=now)))
    )
    for kind, department, n, overdue in open_tasks:
        if kind in _TASK_KINDS:
            out[_TASK_KINDS[kind]] += n
        if kind == "final_balance_paid":
            out["unpaid_balances"] += overdue
        if f"overdue_{department}" in out:
            out[f"overdue_{department}"] += overdue

    unpaid = AffiliatePayable.objects.exclude(status=AffiliatePayable.Status.PAID).aggregate(
        missing=Count(
            "pk", filter=Q(status=AffiliatePayable.Status.DRAFT, invoice_amount__isnull=True)
        ),
        pending=Count("pk", filter=Q(status=AffiliatePayable.Status.APPROVED)),
    )
    out["missing_invoices"] = unpaid["missing"]
    out["pending_payments"] = unpaid["pending"]

    out["open_issues"] = TripIssue.objects.open().count()
    return out


def snapshot(config: TaskConfig, now) -> int:
    """Record today's counts, once per business day. Returns the rows written."""
    today = timezone.localdate(now)
    if config.exceptions_snapshot_on == today:
        return 0
    rows = [ExceptionSnapshot(day=today, key=key, count=n) for key, n in counts(now).items()]
    # A second worker racing the same tick loses quietly on the unique constraint.
    ExceptionSnapshot.objects.bulk_create(rows, ignore_conflicts=True)
    TaskConfig.objects.filter(pk=config.pk).update(exceptions_snapshot_on=today)
    config.exceptions_snapshot_on = today
    return len(rows)


def trend(days: int = TREND_DAYS, today: date | None = None) -> dict[str, list[tuple[date, int]]]:
    """Each view's daily counts over the last `days`, oldest first. One query."""
    today = today or timezone.localdate()
    out: dict[str, list[tuple[date, int]]] = {view.key: [] for view in VIEWS}
    rows = ExceptionSnapshot.objects.filter(day__gt=today - timedelta(days=days), day__lte=today)
    for day, key, n in rows.order_by("day").values_list("day", "key", "count"):
        if key in out:
            out[key].append((day, n))
    return out
