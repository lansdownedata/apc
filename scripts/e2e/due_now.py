"""Make a scheduled message or a task due now, so the next cron run picks it up.

The e2e checklist (sections 8 and 10) needs things that are normally days away: a thank-you
two hours after the trip, a review request a day later, a task overdue past its escalation
grace, the daily digest a second time. This moves the one timestamp the cron reads and
nothing else; the cron (`scripts/local_cron.sh run-touchpoints` / `run-tasks`) does the rest
through the real code.

Subcommands (repo root, venv active):
    python scripts/e2e/due_now.py list --lead APC-100042           # its touch-points and tasks
    python scripts/e2e/due_now.py touchpoint --id 812               # scheduled_for = 1 min ago
    python scripts/e2e/due_now.py touchpoint --lead APC-100042 --kind thank_you
    python scripts/e2e/due_now.py task --id 455                     # due_at = 1 min ago
    python scripts/e2e/due_now.py task --lead 42 --kind final_itinerary --past-grace
    python scripts/e2e/due_now.py digest                            # let today's digest go again
Add --dry-run to any of them to print what would change and roll it back.

Only SCHEDULED touch-points and unresolved (scheduled or open) tasks are touched.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta

from _bootstrap import django_ready


class _Rollback(Exception):
    pass


def _lead_id(raw: str) -> int:
    """A lead pk from `42` or a quote number like `APC-100042`."""
    from apps.leads.models import QUOTE_NUMBER_BASE

    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    m = re.fullmatch(r"[A-Za-z]+-(\d+)", raw)
    if not m:
        sys.exit(f"--lead must be a pk or a quote number like APC-100042, not {raw!r}")
    return int(m.group(1)) - QUOTE_NUMBER_BASE


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Make a scheduled TouchPoint or an open Task due now (local dev DB only)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="List an order's touch-points and tasks with their times.")
    ls.add_argument("--lead", required=True, help="Lead pk or quote number (APC-100042)")

    tp = sub.add_parser("touchpoint", help="Set a SCHEDULED TouchPoint's scheduled_for to now.")
    tp.add_argument("--id", type=int, help="TouchPoint id")
    tp.add_argument("--lead", help="Lead pk or quote number (with --kind)")
    tp.add_argument("--kind", help="TouchPoint kind / message key, e.g. thank_you, custom-12")
    tp.add_argument(
        "--minutes-ago", type=int, default=1, help="How far in the past to set it (default 1)"
    )
    tp.add_argument("--dry-run", action="store_true", help="Show the change, then roll back")

    tk = sub.add_parser("task", help="Set an unresolved Task's due_at (and opens_at) to now.")
    tk.add_argument("--id", type=int, help="Task id")
    tk.add_argument("--lead", help="Lead pk or quote number (with --kind)")
    tk.add_argument("--kind", help="Task kind, e.g. final_itinerary, insurance_renewal")
    tk.add_argument(
        "--minutes-ago", type=int, default=1, help="How long overdue to make it (default 1)"
    )
    tk.add_argument(
        "--past-grace",
        action="store_true",
        help="Overdue by the Settings -> Tasks grace + 1 hour, so tier 2 (Escalated) fires",
    )
    tk.add_argument(
        "--reset-tier",
        action="store_true",
        help="Also set escalated_tier back to 0 so the Overdue notice can fire again",
    )
    tk.add_argument("--dry-run", action="store_true", help="Show the change, then roll back")

    dg = sub.add_parser("digest", help="Clear today's digest stamp so run-tasks sends it again.")
    dg.add_argument("--dry-run", action="store_true", help="Show the change, then roll back")
    return p.parse_args()


def _list(args) -> None:
    from apps.messaging.models import TouchPoint
    from apps.tasks.models import Task

    lead_id = _lead_id(args.lead)
    print(f"Touch-points on lead {lead_id}:")
    for t in TouchPoint.objects.filter(lead_id=lead_id).order_by("scheduled_for", "pk"):
        print(
            f"  tp {t.pk:<6} {t.kind:<26} {t.status:<10} trip={t.reservation_id or '-':<6} "
            f"scheduled_for={t.scheduled_for:%Y-%m-%d %H:%M} UTC  {t.error}"
            if t.scheduled_for
            else f"  tp {t.pk:<6} {t.kind:<26} {t.status:<10} (no time)  {t.error}"
        )
    print(f"Tasks on lead {lead_id}:")
    for t in Task.objects.filter(lead_id=lead_id).order_by("due_at", "pk"):
        due = f"{t.due_at:%Y-%m-%d %H:%M} UTC" if t.due_at else "no due date"
        print(
            f"  task {t.pk:<6} {t.kind:<28} {t.status:<15} trip={t.reservation_id or '-':<6} "
            f"due={due} tier={t.escalated_tier}"
        )


def _touchpoint(args, now) -> None:
    from apps.messaging.models import TouchPoint

    qs = TouchPoint.objects.filter(status=TouchPoint.Status.SCHEDULED)
    if args.id:
        qs = qs.filter(pk=args.id)
    elif args.lead and args.kind:
        qs = qs.filter(lead_id=_lead_id(args.lead), kind=args.kind)
    else:
        sys.exit("Give --id, or --lead and --kind.")
    rows = list(qs)
    if not rows:
        sys.exit("No SCHEDULED touch-point matches (sent, skipped or cancelled ones are left).")
    at = now - timedelta(minutes=args.minutes_ago)
    for t in rows:
        print(f"tp {t.pk} {t.kind} lead={t.lead_id}: scheduled_for {t.scheduled_for} -> {at}")
        t.scheduled_for = at
        t.save(update_fields=["scheduled_for"])  # updated_at untouched: no lease is taken


def _task(args, now) -> None:
    from apps.tasks.models import Task, TaskConfig

    qs = Task.objects.filter(status__in=Task.UNRESOLVED)
    if args.id:
        qs = qs.filter(pk=args.id)
    elif args.lead and args.kind:
        qs = qs.filter(lead_id=_lead_id(args.lead), kind=args.kind)
    elif args.kind:
        qs = qs.filter(kind=args.kind)
    else:
        sys.exit("Give --id, or --kind (optionally with --lead).")
    rows = list(qs)
    if not rows:
        sys.exit("No unresolved task matches (done, skipped and not-applicable ones are left).")
    ago = timedelta(minutes=args.minutes_ago)
    if args.past_grace:
        ago = timedelta(hours=TaskConfig.load().overdue_grace_hours + 1)
    due = now - ago
    for t in rows:
        fields = ["due_at", "updated_at"]
        print(f"task {t.pk} {t.kind} ({t.status}): due_at {t.due_at} -> {due}")
        t.due_at = due
        if t.opens_at > due:
            t.opens_at = due - timedelta(minutes=1)
            fields.append("opens_at")
        if t.status == Task.Status.SCHEDULED:
            t.status = Task.Status.OPEN
            fields.append("status")
            print("   status scheduled -> open")
        if args.reset_tier and t.escalated_tier:
            print(f"   escalated_tier {t.escalated_tier} -> 0")
            t.escalated_tier = 0
            fields.append("escalated_tier")
        t.updated_at = now
        t.save(update_fields=fields)


def _digest(args, now) -> None:
    from apps.tasks.models import TaskConfig

    config = TaskConfig.load()
    print(f"TaskConfig.digest_sent_on {config.digest_sent_on} -> None")
    TaskConfig.objects.filter(pk=config.pk).update(digest_sent_on=None)


def main() -> None:
    args = _parse()
    django_ready()
    from django.db import transaction
    from django.utils import timezone

    if args.cmd == "list":
        _list(args)
        return
    now = timezone.now()
    run = {"touchpoint": _touchpoint, "task": _task, "digest": _digest}[args.cmd]
    try:
        with transaction.atomic():
            run(args, now)
            if args.dry_run:
                raise _Rollback
    except _Rollback:
        print("Dry run: rolled back, nothing saved.")
        return
    print("Saved. Now run the cron, e.g. scripts/local_cron.sh run-touchpoints (or run-tasks).")


if __name__ == "__main__":
    main()
