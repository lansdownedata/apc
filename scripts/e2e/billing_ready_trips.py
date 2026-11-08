"""Make a billing group's on-account trips ready to invoice (checklist section 7, BILL-05).

For each of the group's unprocessed trips on a BOOKED order billed to it (oldest pickup first),
this does what an agent does by hand:

1. Sets the trip's status to Done through `reservations.services.set_trip_status` (the same seam
   the dispatch drawer uses, so the trip's "done" message events queue as they would from a click),
   unless it already has a settled status (Done, Late Cancel, No Show).
2. Processes it through `reservations.processing.process(..., collect="invoice")`, the service
   behind the Process button on /portal/processing/?trip=<id>. No card action is taken.

A trip whose overtime suggestion is still undecided is skipped (decide it in the pane first).

Usage (repo root, venv active):
    python scripts/e2e/billing_ready_trips.py --group 3 --count 3 --dry-run
    python scripts/e2e/billing_ready_trips.py --account 2 --count 5
    python scripts/e2e/billing_ready_trips.py --list
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import django_ready


class _Rollback(Exception):
    pass


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Process N on-account trips of a billing group so they appear on "
        "/portal/billing/ as ready to invoice. Uses the real processing service."
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--group", type=int, help="AccountGroup id")
    target.add_argument("--account", type=int, help="BillingAccount id (uses its default group)")
    target.add_argument(
        "--list",
        action="store_true",
        help="List billing groups with their candidate trips, then exit",
    )
    p.add_argument("--count", type=int, default=3, help="How many trips to process (default 3)")
    p.add_argument(
        "--user",
        default="moe@lansdownedata.com",
        help="Staff user (email) recorded as processor (default moe@lansdownedata.com)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything inside a transaction, print the result, then roll back",
    )
    return p.parse_args()


def _candidates(group):
    from apps.reservations.models import Reservation
    from apps.reservations.processing import NEVER

    return (
        Reservation.objects.filter(
            lead__status="booked",
            lead__billing_group=group,
            pickup_date__isnull=False,
        )
        .exclude(trip_status__in=NEVER)
        .exclude(review__processed_at__isnull=False)
        .select_related("lead")
        .order_by("pickup_date", "pickup_time", "pk")
    )


def _list() -> None:
    from apps.billing.models import AccountGroup

    groups = AccountGroup.objects.select_related("account").order_by("account__name", "name")
    if not groups:
        print("No billing groups yet. Create one on a contact's Billing card (BILL-01).")
        return
    for g in groups:
        n = _candidates(g).count()
        print(f"group {g.pk:>4}  {g.account.name} - {g.name}  ({n} unprocessed booked trip(s))")


def _run(group, count: int, user) -> list[str]:
    from apps.payments import trip_money
    from apps.reservations import processing, reviews
    from apps.reservations import services as res_services
    from apps.reservations.models import Reservation

    done = Reservation.TripStatus.DONE
    out: list[str] = []
    processed = 0
    for trip in _candidates(group):
        if processed >= count:
            break
        label = f"trip #{trip.reference} (order {trip.lead.quote_no}, pickup {trip.pickup_date})"
        if trip.trip_status not in processing.SETTLE_STATUSES:
            res_services.set_trip_status(trip, done, user=user)
            trip.refresh_from_db()
        review = reviews.review_for(trip)
        if review.decided_at is None and review.suggested_overtime_minutes > 0:
            out.append(f"SKIP {label}: overtime suggestion undecided; decide it in the pane.")
            continue
        money = trip_money.money_for(trip)
        collect = "invoice" if money.difference > 0 else ""
        try:
            processing.process(trip, user=user, expected=money.difference, collect=collect)
        except processing.ProcessingError as exc:
            out.append(f"SKIP {label}: {exc}")
            continue
        processed += 1
        out.append(f"OK   {label}: processed, ${trip.line_total:,.2f} owed to the invoice")
    if processed < count:
        out.append(f"Only {processed} of {count} trips could be processed for this group.")
    return out


def main() -> None:
    args = _parse()
    django_ready()
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from apps.billing.models import AccountGroup, BillingAccount

    if args.list:
        _list()
        return
    user = get_user_model().objects.filter(email__iexact=args.user).first()
    if user is None:
        sys.exit(f"No user with email {args.user!r}.")
    if args.group:
        group = AccountGroup.objects.select_related("account").filter(pk=args.group).first()
    else:
        account = BillingAccount.objects.filter(pk=args.account).first()
        group = account.default_group if account else None
    if group is None:
        sys.exit("No such billing group/account. Try --list.")
    print(f"Group: {group.account.name} - {group.name} (id {group.pk})")
    try:
        with transaction.atomic():
            lines = _run(group, args.count, user)
            for line in lines:
                print(line)
            if args.dry_run:
                raise _Rollback
    except _Rollback:
        print("DRY RUN: rolled back, nothing saved.")
        return
    print(f"Open /portal/billing/?group={group.pk} to invoice them.")


if __name__ == "__main__":
    main()
