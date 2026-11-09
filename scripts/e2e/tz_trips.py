"""Seed one order whose two trips sit in different timezones, for the timezone checks (section 13).

Creates (or reuses, by email) a contact and one order holding:

  LA leg    Airport Transfer, pickup at LAX (airport row, America/Los_Angeles) at --la-time
            (default 10:00) on day D, to a Beverly Hills hotel.
  late ET   Airport Transfer, pickup at The Hay-Adams, Washington DC (coordinates, so the zone
            resolves to America/New_York) at --late-time (default 23:30) on day D, to DCA.
            At 11:30 PM EDT/EST it is already day D+1 in UTC: the date-boundary case.

Day D is --days-out days from today (default 10). Each trip's zone is resolved the way the
editor does it (`Reservation.refresh_pickup_timezone`), never typed in. Unless --no-book, the
order is booked through the real `book_lead` (plan snapshot, tasks, scheduled messages, the
LimoAnywhere preview push) exactly as the workspace's Book now does.

No phone is put on a new contact, so no text can go anywhere. Booking with OUTBOUND_ENABLED and
TOUCHPOINTS_ENABLED on writes the customer's mail to .local-mail/ (file backend) and nothing else.
Vehicles are picked from types with no cleaning fee (no cleaning hold).

Usage (repo root, venv active):
  python scripts/e2e/tz_trips.py --dry-run
  python scripts/e2e/tz_trips.py --email moe+customer@lansdownedata.com
  python scripts/e2e/tz_trips.py --email moe+customer@lansdownedata.com --days-out 3 --no-book
"""

from __future__ import annotations

import argparse
from datetime import date, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from _bootstrap import BASE_URL, django_ready

HAY_ADAMS = ("The Hay-Adams", "800 16th St NW, Washington, DC 20006", "38.900700", "-77.036700")
BEVERLY = (
    "The Beverly Hills Hotel",
    "9641 Sunset Blvd, Beverly Hills, CA 90210",
    "34.081800",
    "-118.413400",
)


def parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seed an order with an LAX trip and an 11:30 PM Eastern trip.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--email",
        default="moe+customer@lansdownedata.com",
        help="contact email (reused if a contact already has it)",
    )
    p.add_argument("--name", default="Tess Zone (E2E)", help="contact name for a new contact")
    p.add_argument(
        "--agent-email",
        default="moe@lansdownedata.com",
        help="staff user set as the assigned agent",
    )
    p.add_argument("--days-out", type=int, default=10, help="days from today to both trips")
    p.add_argument("--la-time", default="10:00", help="LAX pickup, LA wall-clock HH:MM")
    p.add_argument("--late-time", default="23:30", help="DC pickup, Eastern wall-clock HH:MM")
    p.add_argument("--no-book", action="store_true", help="leave the order New (book it in the UI)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything in a transaction, print it, then roll back",
    )
    return p.parse_args()


def _clock(raw: str) -> time:
    hh, mm = raw.split(":")
    return time(int(hh), int(mm))


def main() -> None:
    args = parse()
    django_ready()

    from django.db import transaction

    from apps.accounts.models import User
    from apps.addresses.models import Airport
    from apps.contacts.models import Contact
    from apps.core.choices import Channel
    from apps.leads import services as lead_services
    from apps.leads.models import Lead, ServiceType, VehicleType
    from apps.reservations import acknowledgements as ack
    from apps.reservations.models import Reservation, Stop

    lax = Airport.objects.filter(iata="LAX").first()
    dca = Airport.objects.filter(iata="DCA").first()
    if lax is None or dca is None:
        raise SystemExit("LAX / DCA airport rows are missing (run seed_airports).")
    vehicle = (
        VehicleType.objects.filter(active=True, cleaning_fee=0)
        .exclude(overtime_class=VehicleType.OvertimeClass.LARGE)
        .order_by("capacity", "sort_order")
        .first()
    )
    if vehicle is None:
        raise SystemExit("No active vehicle type without a cleaning fee.")
    service, _ = ServiceType.objects.get_or_create(name="Airport Transfer")
    agent = User.objects.filter(email__iexact=args.agent_email).first()
    day = date.today() + timedelta(days=args.days_out)

    with transaction.atomic():
        contact = Contact.objects.match_or_create(
            name=args.name, email=args.email, channel=Channel.PHONE
        )
        lead = Lead.objects.create(
            contact=contact,
            assigned_agent=agent,
            channel=Channel.PHONE,
            status=Lead.Status.NEW,
            notes="E2E timezone order (scripts/e2e/tz_trips.py).",
        )

        def trip(clock: time, stops: list[dict]) -> Reservation:
            res = Reservation.objects.create(
                lead=lead,
                vehicle=vehicle,
                trip_type=Reservation.TripType.TRANSFER,
                service_type=service,
                pickup_date=day,
                pickup_time=clock,
                passengers=2,
                rate=vehicle.rate or Decimal("150"),
                hours=Decimal("1"),
            )
            for i, s in enumerate(stops):
                Stop.objects.create(reservation=res, sequence=i, **s)
            res.refresh_pickup_timezone()
            return res

        def airport_stop(a, **extra) -> dict:
            return {
                "name": a.name,
                "address": f"{a.name} ({a.iata})",
                "airport": a,
                "latitude": a.latitude,
                "longitude": a.longitude,
                **extra,
            }

        def place(spec) -> dict:
            name, address, lat, lng = spec
            return {
                "name": name,
                "address": address,
                "latitude": Decimal(lat),
                "longitude": Decimal(lng),
            }

        la = trip(
            _clock(args.la_time),
            [airport_stop(lax, scheduled_time=_clock(args.la_time)), place(BEVERLY)],
        )
        late = trip(
            _clock(args.late_time),
            [{**place(HAY_ADAMS), "scheduled_time": _clock(args.late_time)}, airport_stop(dca)],
        )

        if not args.no_book:
            lead_services.book_lead(lead)
        lead.refresh_from_db()

        print(
            f"{'DRY RUN, rolled back: ' if args.dry_run else ''}order {lead.quote_no} "
            f"(lead {lead.pk}) status={lead.status} contact={contact.email} ({contact.pk})"
        )
        for label, t in (("LA leg", la), ("late ET", late)):
            t.refresh_from_db()
            utc = t.pickup_at.astimezone(ZoneInfo("UTC"))
            print(
                f"  {label:8} trip {t.pk}: {t.pickup_date} {t.pickup_time:%H:%M} "
                f"zone={t.pickup_timezone or '(blank)'} label={t.pickup_tz_label} "
                f"staff-abbrev={t.pickup_tz_abbrev or '(none)'} UTC={utc:%Y-%m-%d %H:%M}"
            )
        token = lead_services.make_deposit_token(lead)
        print(f"  workspace   {BASE_URL}/portal/leads/{lead.pk}/")
        print(f"  order       {BASE_URL}/portal/orders/{lead.pk}/")
        print(f"  board       {BASE_URL}/portal/dispatch/?view=day&day={day.isoformat()}")
        print(
            f"  board D+1   {BASE_URL}/portal/dispatch/?view=day&day="
            f"{(day + timedelta(days=1)).isoformat()}"
        )
        print(f"  quote page  {BASE_URL}/quote/{token}/")
        print(f"  trip confirm (day D)  {ack.trip_day_ack_url(contact, day, base_url=BASE_URL)}")
        if args.dry_run:
            transaction.set_rollback(True)


if __name__ == "__main__":
    main()
