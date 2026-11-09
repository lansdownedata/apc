"""Move an order's trips in time, the way the reservation editor would, so date-driven flows
(balance cron at T-30, 72h/48h confirmations, T-24h, past trips for revenue recognition,
cleaning holds on the trip day) can be tested today instead of weeks from now.

Shared by several checklist sections (payments, trip lifecycle, cleaning holds).

WHAT IT CHANGES (inside one transaction):
  * Reservation.pickup_date / pickup_time and dropoff_date / dropoff_time. Each trip keeps
    its own pickup timezone (`pickup_timezone` is not touched) and its duration.
      --days N / --date D : calendar shift. Dates move, wall-clock times stay as they are.
      --hours H           : absolute shift. The FIRST trip's pickup lands H hours from now
                            (in its own zone); every other selected trip, its dropoff and its
                            stops' scheduled times move by the same amount.
      --by N              : shift every selected trip by N calendar days (no anchor).
  * Stop.flight (the aviationstack cache link) is re-derived for the new date with
    `reservations.flights.link_flights`, exactly as an editor save does.
  * Booked orders only, as `reservations.drafts.save_reservation` does after a pickup moves:
      messaging.events.reschedule_trip(trip) + events.sync_waiting(trip)   (per trip)
      tasks.services.ensure_tasks(lead) + tasks.services.reschedule(lead)  (per order)
    reschedule_trip WITHDRAWS the trip's pickup messages (sent ones too), clears the customer
    and affiliate confirmations, and queues them afresh. A row whose new send time is already
    past goes out ON COMMIT (TOUCHPOINTS_ENABLED + OUTBOUND_ENABLED are on locally, so that is
    real mail in .local-mail/ and real Podium API calls to the test location).
  * The order's queued "balance due" date rows (the Payment reminder, T-30 minus 72h) are
    re-dated to their anchor. NOTE: the product itself does NOT do this when a pickup moves
    in the editor; pass --no-order-rows to behave exactly like the editor.

WHAT IT DOES NOT TOUCH: PaymentPlan / Charge rows (the balance due date is derived from the
earliest pickup_date, so it follows automatically), cleaning-hold rows, the quote expiry (use
`scripts/e2e/leads_quote_tools.py expiry <lead> --recompute`), trip statuses.

Order: a lead pk (42) or a quote number (APC-100042 / 100042).

Examples (repo root, venv active):
  python scripts/e2e/shift_trip_dates.py APC-100042 --show
  python scripts/e2e/shift_trip_dates.py APC-100042 --days 29 --dry-run     # balance cron
  python scripts/e2e/shift_trip_dates.py APC-100042 --days 29
  python scripts/e2e/shift_trip_dates.py 42 --days 1                         # T-24h / 72h
  python scripts/e2e/shift_trip_dates.py 42 --days -1                        # a past trip
  python scripts/e2e/shift_trip_dates.py 42 --hours 2 --trip 311             # one trip, 2h out
  python scripts/e2e/shift_trip_dates.py 42 --date 2026-12-31
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from _bootstrap import django_ready


class _Rollback(Exception):
    pass


def _lead_id(raw: str) -> int:
    from apps.leads.models import QUOTE_NUMBER_BASE

    raw = raw.strip()
    m = re.fullmatch(r"(?:[A-Za-z]+-)?(\d+)", raw)
    if not m:
        sys.exit(f"ORDER must be a lead pk or a quote number like APC-100042, not {raw!r}")
    n = int(m.group(1))
    if "-" in raw or n >= QUOTE_NUMBER_BASE:
        return n - QUOTE_NUMBER_BASE
    return n


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Shift an order's trips so the first pickup is N days (or H hours) out, the way "
            "the reservation editor would. Local dev DB only. See the module docstring."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples", 1)[1] if "Examples" in __doc__ else "",
    )
    p.add_argument("order", help="Lead pk (42) or quote number (APC-100042)")
    when = p.add_mutually_exclusive_group()
    when.add_argument(
        "--days", type=int, help="First selected pickup lands N days from today (trip-zone today)"
    )
    when.add_argument("--date", help="First selected pickup lands on this date (YYYY-MM-DD)")
    when.add_argument(
        "--hours", type=float, help="First selected pickup lands H hours from now (absolute)"
    )
    when.add_argument("--by", type=int, help="Shift every selected trip by N calendar days")
    p.add_argument(
        "--trip",
        action="append",
        default=[],
        help="Only this trip (pk or reference); repeatable. Default: every trip on the order",
    )
    p.add_argument("--include-cancelled", action="store_true", help="Also move cancelled trips")
    p.add_argument(
        "--no-order-rows",
        action="store_true",
        help="Leave the queued Payment reminder where it is (exactly what the editor does)",
    )
    p.add_argument("--show", action="store_true", help="Print the trips and derived dates only")
    p.add_argument("--dry-run", action="store_true", help="Do everything, print it, then roll back")
    a = p.parse_args()
    if not a.show and a.days is None and a.date is None and a.hours is None and a.by is None:
        p.error("give one of --days, --date, --hours, --by (or --show)")
    return a


def _zone(trip) -> ZoneInfo:
    from django.conf import settings

    return ZoneInfo(trip.pickup_timezone or settings.TIME_ZONE)


def _fmt(trip) -> str:
    when = trip.pickup_at
    clock = when.strftime("%a %b %-d %Y %-I:%M %p %Z") if when else "no pickup date"
    drop = ""
    if trip.dropoff_date or trip.dropoff_time:
        drop = f" -> drop {trip.dropoff_date or ''} {trip.dropoff_time or ''}".rstrip()
    return f"trip {trip.pk} {trip.reference} [{trip.trip_status}] {clock}{drop}"


def _show(lead, trips) -> None:
    plan = getattr(lead, "payment", None)
    print(f"{lead.quote_no} (lead {lead.pk}) status={lead.status} on_account={lead.on_account}")
    for t in trips:
        print("  " + _fmt(t))
    if plan is None:
        print("  no PaymentPlan")
        return
    plan = type(plan).objects.get(pk=plan.pk)  # fresh: earliest_pickup reads the trips
    print(
        f"  plan: deposit={plan.deposit_status} balance={plan.balance_status} "
        f"card={'yes' if plan.stripe_payment_method_id else 'no'} "
        f"balance_due_date={plan.balance_due_date} balance_due_now={plan.balance_due_now} "
        f"(server date.today()={date.today()})"
    )


def _select(lead, wanted: list[str], include_cancelled: bool):
    trips = list(lead.reservations.all().order_by("pickup_date", "pickup_time", "pk"))
    if wanted:
        keys = {w.strip() for w in wanted}
        trips = [t for t in trips if str(t.pk) in keys or t.reference in keys]
        if len(trips) != len(keys):
            sys.exit(f"--trip: not all of {sorted(keys)} are trips on {lead.quote_no}")
    elif not include_cancelled:
        trips = [t for t in trips if not t.is_cancelled]
    return trips


def _relink_flights(trip) -> int:
    from apps.reservations.flights import link_flights

    stops = list(trip.stops.all())
    dicts = [
        {
            "airport_id": s.airport_id,
            "airline_id": s.airline_id,
            "flight_number": s.flight_number,
            "flight_direction": s.flight_direction,
        }
        for s in stops
    ]
    link_flights(dicts, trip.pickup_date)
    changed = 0
    for stop, d in zip(stops, dicts, strict=True):
        if stop.flight_id != d["flight_id"]:
            stop.flight_id = d["flight_id"]
            stop.save(update_fields=["flight", "updated_at"])
            changed += 1
    return changed


def _shift_calendar(trip, days: int) -> None:
    trip.pickup_date = trip.pickup_date + timedelta(days=days)
    if trip.dropoff_date:
        trip.dropoff_date = trip.dropoff_date + timedelta(days=days)


def _plus(moment: datetime, delta: timedelta, zone: ZoneInfo) -> datetime:
    """`moment + delta` as elapsed time (via UTC), read back on `zone`'s wall clock. Plain
    aware arithmetic would add to the wall clock and come out an hour off across DST."""
    return (moment.astimezone(UTC) + delta).astimezone(zone)


def _shift_absolute(trip, delta: timedelta) -> None:
    zone = _zone(trip)
    old_pickup = datetime.combine(trip.pickup_date, trip.pickup_time or time(0, 0), tzinfo=zone)
    new_pickup = _plus(old_pickup, delta, zone)
    if trip.dropoff_time is not None:
        base = trip.dropoff_date or trip.pickup_date
        old_drop = datetime.combine(base, trip.dropoff_time, tzinfo=zone)
        if trip.dropoff_date is None and old_drop < old_pickup:
            old_drop += timedelta(days=1)  # an overnight run with no explicit dropoff date
        new_drop = _plus(old_drop, delta, zone)
        trip.dropoff_time = new_drop.time().replace(tzinfo=None)
        if trip.dropoff_date is not None:
            trip.dropoff_date = new_drop.date()
    elif trip.dropoff_date is not None:
        trip.dropoff_date = _plus(
            datetime.combine(trip.dropoff_date, time(12, 0), tzinfo=zone), delta, zone
        ).date()
    for stop in trip.stops.exclude(scheduled_time=None):
        moved = _plus(
            datetime.combine(trip.pickup_date, stop.scheduled_time, tzinfo=zone), delta, zone
        )
        stop.scheduled_time = moved.time().replace(tzinfo=None)
        stop.save(update_fields=["scheduled_time", "updated_at"])
    trip.pickup_date = new_pickup.date()
    if trip.pickup_time is not None:
        trip.pickup_time = new_pickup.time().replace(tzinfo=None, second=0, microsecond=0)


def _redate_order_rows(lead) -> int:
    """Queued order-level date rows (balance due) back on their anchor + offset."""
    from apps.messaging import triggers
    from apps.messaging.models import TouchPoint

    lead = type(lead).objects.select_related("payment").get(pk=lead.pk)
    trigger = triggers.get("balance_due")
    anchor = trigger.anchor(lead, None)
    if anchor is None:
        return 0
    changed = 0
    for tp in TouchPoint.objects.filter(
        lead=lead,
        reservation=None,
        status=TouchPoint.Status.SCHEDULED,
        message__trigger="balance_due",
    ).select_related("message"):
        at = anchor + tp.message.offset
        if tp.scheduled_for != at:
            print(f"  re-dated touch-point {tp.pk} ({tp.kind}) {tp.scheduled_for} -> {at}")
            TouchPoint.objects.filter(pk=tp.pk).update(scheduled_for=at)
            changed += 1
    return changed


def main() -> None:
    args = _parse()
    django_ready()
    from django.db import transaction
    from django.utils import timezone

    from apps.leads.models import Lead

    lead = Lead.objects.filter(pk=_lead_id(args.order)).select_related("contact").first()
    if lead is None:
        sys.exit(f"No lead {args.order!r}")
    trips = _select(lead, args.trip, args.include_cancelled)
    dated = [t for t in trips if t.pickup_date is not None]

    if args.show:
        _show(lead, trips)
        return
    if not dated:
        sys.exit(f"{lead.quote_no}: none of the selected trips has a pickup date")
    for t in trips:
        if t.pickup_date is None:
            print(f"  skipping trip {t.pk} {t.reference}: no pickup date")

    first = dated[0]
    if args.hours is not None:
        # Whole minutes, so every shifted clock stays on the minute.
        target = (timezone.now() + timedelta(hours=args.hours)).replace(second=0, microsecond=0)
        delta = target - first.pickup_at
        mode = f"absolute shift of {delta}"
    else:
        if args.by is not None:
            days = args.by
        elif args.date is not None:
            days = (date.fromisoformat(args.date) - first.pickup_date).days
        else:
            today = datetime.now(_zone(first)).date()
            days = (today + timedelta(days=args.days) - first.pickup_date).days
        mode = f"calendar shift of {days:+d} days"

    print(f"{lead.quote_no} (lead {lead.pk}, {lead.status}): {mode}")
    print("BEFORE")
    _show(lead, trips)

    try:
        with transaction.atomic():
            booked = lead.status == Lead.Status.BOOKED
            for trip in dated:
                if args.hours is not None:
                    _shift_absolute(trip, delta)
                else:
                    _shift_calendar(trip, days)
                trip.save(
                    update_fields=[
                        "pickup_date",
                        "pickup_time",
                        "dropoff_date",
                        "dropoff_time",
                        "updated_at",
                    ]
                )
                relinked = _relink_flights(trip)
                if relinked:
                    print(f"  trip {trip.pk}: {relinked} stop flight link(s) re-derived")
            if booked:
                from apps.messaging import events
                from apps.tasks import services as tasks

                for trip in dated:
                    trip.refresh_from_db()
                    events.reschedule_trip(trip)
                    events.sync_waiting(trip)
                tasks.ensure_tasks(lead)
                print(f"  tasks re-dated: {tasks.reschedule(lead)}")
                if not args.no_order_rows:
                    print(f"  order date rows re-dated: {_redate_order_rows(lead)}")
                print(
                    "  pickup messages withdrawn and re-queued; any already due go out on "
                    "commit" + (" (rolled back: dry run)" if args.dry_run else "")
                )
            else:
                print("  not booked: no messages or tasks to re-date (as the editor)")
            lead.refresh_from_db()
            print("AFTER")
            _show(lead, [type(t).objects.get(pk=t.pk) for t in trips])
            if args.dry_run:
                raise _Rollback
    except _Rollback:
        print("DRY RUN: rolled back, nothing saved, nothing sent.")


if __name__ == "__main__":
    main()
