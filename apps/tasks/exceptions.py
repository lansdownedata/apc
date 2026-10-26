"""The exception dashboard's numbers (APC-69): "what's wrong right now?"

Saved views over data that already exists, not new reports. `counts()` answers every view
in a fixed handful of aggregate queries; `snapshot()` records them once a business day so
the dashboard can show a 30-day trend (`trend()`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department
from apps.dispatch.models import DispatchException
from apps.payments.models import AffiliatePayable
from apps.payments.reports import unpaid_deposit_leads

from .models import ExceptionSnapshot, Task, TaskConfig

TREND_DAYS = 30


@dataclass(frozen=True)
class View:
    key: str
    label: str
    group: str
    # Where a click drills down to: Tasks-queue filters (the default), or a named route
    # with its own query. The queue always opens on everyone's tasks, not just mine.
    filters: tuple[tuple[str, str], ...] = ()
    route: str = "task_queue"
    # Dispatch views carry two numbers: every trip still waiting on this check (open tasks
    # of `backlog_kind` — exactly the list the row drills into), and how many of those the
    # dispatch monitor has flagged as close to pickup (the view's own count).
    backlog_kind: str = ""

    @property
    def backlog_key(self) -> str:
        return f"all_{self.key}"

    @property
    def url(self) -> str:
        query = dict(self.filters)
        if self.route == "task_queue":
            query.setdefault("assignee", "anyone")
        return f"{reverse(self.route)}?{urlencode(query)}" if query else reverse(self.route)


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


def _kind(kind: str, **more: str) -> tuple[tuple[str, str], ...]:
    return (("kind", kind), *more.items())


OVERDUE_GROUP = "Overdue tasks"

VIEWS: tuple[View, ...] = (
    View(
        "no_coverage",
        "Not covered yet",
        "Dispatch",
        _kind("affiliate_assigned"),
        backlog_kind="affiliate_assigned",
    ),
    View(
        "affiliate_unconfirmed",
        "Awaiting affiliate confirmation",
        "Dispatch",
        _kind("affiliate_confirmed"),
        backlog_kind="affiliate_confirmed",
    ),
    View(
        "no_driver_info",
        "Driver info missing",
        "Dispatch",
        _kind("driver_info_received"),
        backlog_kind="driver_info_received",
    ),
    View(
        "missing_day_of_contact",
        "Day-of contact missing",
        "Customer details",
        _kind("day_of_contact"),
    ),
    View(
        "missing_wedding_names", "Wedding names missing", "Customer details", _kind("wedding_names")
    ),
    View(
        "open_issues",
        "Open trip issues",
        "Customer details",
        (("issues", "1"),),
        "processing",
    ),
    View("unpaid_deposits", "Unpaid deposits", "Money", _kind("deposit_received")),
    View("unpaid_balances", "Unpaid balances", "Money", _kind("final_balance_paid", due="overdue")),
    View(
        "overtime_awaiting",
        "Overtime awaiting decision",
        "Money",
        (),
        "processing",
    ),
    View(
        "missing_invoices",
        "Affiliate invoices missing",
        "Money",
        _kind("affiliate_payable_approved"),
    ),
    View("pending_payments", "Affiliate payments pending", "Money", _kind("affiliate_paid")),
    *(
        View(f"overdue_{value}", label, OVERDUE_GROUP, (("department", value), ("due", "overdue")))
        for value, label in Department.choices
    ),
)
GROUPS = tuple(dict.fromkeys(view.group for view in VIEWS))
# The group headings that have one obvious place to go.
_GROUP_URLS = {OVERDUE_GROUP: (("due", "overdue"), ("assignee", "anyone"))}


_BACKLOG = {view.backlog_kind: view.backlog_key for view in VIEWS if view.backlog_kind}


def counts(now=None) -> dict[str, int]:
    """Every view's open count. Five queries, whatever the volume."""
    return _measure(now or timezone.now())[0]


def _measure(now) -> tuple[dict[str, int], dict[str, int]]:
    """(each view's count, each dispatch view's backlog) from the same five queries."""
    out = {view.key: 0 for view in VIEWS}
    backlog = dict.fromkeys(_BACKLOG.values(), 0)

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
        if kind in _BACKLOG:
            backlog[_BACKLOG[kind]] += n
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

    # Trips with an open issue: what the processing list's issues filter shows (APC-102).
    from apps.reservations import processing

    out["open_issues"] = processing.issue_trip_count()
    return out, backlog


def snapshot(config: TaskConfig, now) -> int:
    """Record today's counts, once per business day. Returns the rows written."""
    today = timezone.localdate(now)
    if config.exceptions_snapshot_on == today:
        return 0
    current, backlog = _measure(now)
    rows = [
        ExceptionSnapshot(day=today, key=key, count=n) for key, n in (current | backlog).items()
    ]
    # A second worker racing the same tick loses quietly on the unique constraint.
    ExceptionSnapshot.objects.bulk_create(rows, ignore_conflicts=True)
    TaskConfig.objects.filter(pk=config.pk).update(exceptions_snapshot_on=today)
    config.exceptions_snapshot_on = today
    return len(rows)


def trend(days: int = TREND_DAYS, today: date | None = None) -> dict[str, list[tuple[date, int]]]:
    """Each view's daily counts over the last `days`, oldest first. One query."""
    today = today or timezone.localdate()
    out: dict[str, list[tuple[date, int]]] = {view.key: [] for view in VIEWS}
    out.update({key: [] for key in _BACKLOG.values()})
    rows = ExceptionSnapshot.objects.filter(day__gt=today - timedelta(days=days), day__lte=today)
    for day, key, n in rows.order_by("day").values_list("day", "key", "count"):
        if key in out:
            out[key].append((day, n))
    return out


def _points(series: list[int], width: int, height: int) -> str:
    """An SVG polyline for `series`, oldest first, drawn to one scale inside the box."""
    if len(series) < 2:
        return ""
    top, pad = max(max(series), 1), 3
    step = width / (len(series) - 1)
    return " ".join(
        f"{i * step:.1f},{height - pad - (n / top) * (height - 2 * pad):.1f}"
        for i, n in enumerate(series)
    )


def _week_delta(history: list[tuple[date, int]], current: int, today: date) -> int | None:
    """Today against the snapshot a week ago; None until there is one."""
    then = dict(history).get(today - timedelta(days=7))
    return None if then is None else current - then


def dashboard(now=None) -> dict:
    """Everything the Exceptions page and the summary card render: the headline number
    and its trend, each group's views worst first, and the views with nothing open.

    The headline leaves out the overdue-by-department views — those restate tasks the
    other views already count — and, for the dispatch views, counts only what the monitor
    has flagged as close to pickup: it is the "needs someone today" number, and one
    uncovered trip three weeks out is open on three dispatch checks at once.
    Six queries (five to measure, one for the trend).
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    current, backlog = _measure(now)
    history = trend(today=today)

    headline = [v.key for v in VIEWS if v.group != OVERDUE_GROUP]
    by_day: dict[date, int] = {}
    for key in headline:
        for day, n in history[key]:
            by_day[day] = by_day.get(day, 0) + n
    total = sum(current[key] for key in headline)
    by_day[today] = total  # the live number is today's point, snapshot or not
    total_series = sorted(by_day.items())

    groups, quiet = [], []
    for name in GROUPS:
        rows = []
        for view in (v for v in VIEWS if v.group == name):
            flagged = current[view.key]
            # A dispatch row shows its whole backlog, with the flagged part called out.
            key = view.backlog_key if view.backlog_kind else view.key
            n = max(backlog[key], flagged) if view.backlog_kind else flagged
            if not n:
                label = f"Overdue: {view.label}" if name == OVERDUE_GROUP else view.label
                quiet.append(label)
                continue
            series = [c for day, c in history[key] if day != today] + [n]
            rows.append(
                {
                    "label": view.label,
                    "count": n,
                    "flagged": flagged if view.backlog_kind else 0,
                    "url": view.url,
                    "points": _points(series, 120, 26),
                    "delta": _week_delta(history[key], n, today),
                }
            )
        if rows:
            rows.sort(key=lambda r: (-r["flagged"], -r["count"]))
            link = _GROUP_URLS.get(name)
            groups.append(
                {
                    "label": name,
                    "total": sum(r["count"] for r in rows),
                    "url": f"{reverse('task_queue')}?{urlencode(link)}" if link else "",
                    "rows": rows,
                }
            )
    return {
        "total": total,
        "delta": _week_delta(total_series, total, today),
        "points": _points([n for _, n in total_series], 600, 72),
        "first_day": total_series[0][0],
        "has_trend": len(total_series) > 1,
        "groups": groups,
        "quiet": quiet,
    }
