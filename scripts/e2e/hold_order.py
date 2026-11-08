"""Seed a booked card order that the cleaning hold (APC-95) will act on, or mark its trips Done.

    python scripts/e2e/hold_order.py create --email moe@lansdownedata.com [options]
    python scripts/e2e/hold_order.py done --lead 123 [--trip 456]

`create` makes (or, with --lead, converts) a booked card order for the contact with that email:
hourly trips in a LARGE vehicle type whose cleaning fee is above 0, the first one today (or
tomorrow) in the zone you pick, and a Stripe test card saved on the order's plan the way the
card vault stores one (the contact's own Stripe customer, the PaymentMethod attached to it,
brand/last4 on the plan, a SavedCard row). Booking goes through `leads.services.book_lead`, so
booked totals freeze, tasks open and any confirmation already due goes out (file mail).

  --card visa      pm_card_visa: the hold places and captures.
  --card decline   pm_card_chargeCustomerFail: attaches fine, every charge (the hold too) declines.
  --no-card        no card on the plan: the cron opens "Cleaning hold not placed".

`done` sets the order's trips (or one trip) to Done through `set_trip_status`, the same seam the
LA webhook and the processing pane use, so the trip enters Trip Processing straight away.

--dry-run does everything inside a transaction and rolls it back; it makes no Stripe call.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from _bootstrap import BASE_URL, django_ready

TEST_CARDS = {"visa": "pm_card_visa", "decline": "pm_card_chargeCustomerFail"}

# Two plausible stops per zone, so the trip sheet, the board and the receipt read sensibly.
STOPS = {
    "America/New_York": [
        ("The Jefferson", "1200 16th St NW, Washington, DC 20036"),
        ("Congressional Country Club", "8500 River Rd, Bethesda, MD 20817"),
    ],
    "America/Chicago": [
        ("The Langham", "330 N Wabash Ave, Chicago, IL 60611"),
        ("Medinah Country Club", "6N001 Medinah Rd, Medinah, IL 60157"),
    ],
    "America/Denver": [
        ("The Brown Palace", "321 17th St, Denver, CO 80202"),
        ("Red Rocks Amphitheatre", "18300 W Alameda Pkwy, Morrison, CO 80465"),
    ],
    "America/Los_Angeles": [
        ("The Beverly Hills Hotel", "9641 Sunset Blvd, Beverly Hills, CA 90210"),
        ("Hotel del Coronado", "1500 Orange Ave, Coronado, CA 92118"),
    ],
}


class Abort(Exception):
    pass


class Rollback(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Seed a booked card order for the cleaning-hold flow, or mark its trips Done.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 2)[2],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Create (or convert) a booked, held card order.")
    c.add_argument("--email", help="The contact's email (found case-insensitively).")
    c.add_argument("--name", default="E2E Hold Test", help="Name for a contact created new.")
    c.add_argument("--phone", default="", help="Phone for a contact created new (E.164).")
    c.add_argument("--lead", type=int, help="Convert this existing order instead of creating one.")
    c.add_argument(
        "--vehicle", help="Vehicle type name (default: first active LARGE type with a fee)."
    )
    c.add_argument(
        "--vehicle2", help="A second vehicle type for trip 2 (e.g. van $250 + bus $500)."
    )
    c.add_argument(
        "--fee",
        type=Decimal,
        help="Set this cleaning fee on --vehicle (writes Settings; any overtime class).",
    )
    c.add_argument("--fee2", type=Decimal, help="Set this cleaning fee on --vehicle2.")
    c.add_argument("--zone", default="America/New_York", help="IANA zone of the pickups.")
    c.add_argument(
        "--day", choices=("today", "tomorrow"), default="today", help="First trip's day."
    )
    c.add_argument("--time", help="First pickup, HH:MM in the zone (default: 2 hours from now).")
    c.add_argument("--trips", type=int, default=1, help="Number of trips, one day apart (1-4).")
    c.add_argument("--hours", type=Decimal, default=Decimal("4"), help="Billed hours per trip.")
    c.add_argument(
        "--rate", type=Decimal, help="Hourly rate (default: the vehicle's rate, else 200)."
    )
    c.add_argument("--gratuity", type=Decimal, default=Decimal("20"), help="Gratuity %%.")
    card = c.add_mutually_exclusive_group()
    card.add_argument("--card", choices=sorted(TEST_CARDS), default="visa", help="Test card.")
    card.add_argument("--no-card", action="store_true", help="Leave the order with no card.")
    c.add_argument(
        "--prepaid",
        action="store_true",
        help="Also charge the whole order total to the card now (visa only), so processing "
        "only owes what changes.",
    )
    c.add_argument("--dry-run", action="store_true", help="Roll back; no Stripe calls.")

    d = sub.add_parser("done", help="Set an order's trips (or one trip) to Done.")
    d.add_argument("--lead", type=int, required=True)
    d.add_argument("--trip", type=int, help="Only this trip (pk).")
    d.add_argument("--dry-run", action="store_true", help="Roll back.")
    return p


def _vehicle(name: str | None, fee: Decimal | None, VehicleType):
    if name:
        vt = VehicleType.objects.filter(name__iexact=name).first()
        if vt is None:
            raise Abort(f"No vehicle type named {name!r}.")
    else:
        vt = (
            VehicleType.objects.filter(
                active=True,
                overtime_class=VehicleType.OvertimeClass.LARGE,
                cleaning_fee__gt=0,
            )
            .order_by("sort_order", "name")
            .first()
        )
        if vt is None and fee is not None:
            vt = (
                VehicleType.objects.filter(
                    active=True, overtime_class=VehicleType.OvertimeClass.LARGE
                )
                .order_by("sort_order", "name")
                .first()
            )
        if vt is None:
            raise Abort(
                "No active LARGE vehicle type with a cleaning fee. Set one at "
                "/portal/settings/vehicle-types/ or pass --vehicle NAME --fee 250."
            )
    if fee is not None and Decimal(vt.cleaning_fee) != fee:
        print(f"  setting {vt.name} cleaning fee {vt.cleaning_fee} -> {fee}")
        vt.cleaning_fee = fee
        vt.save(update_fields=["cleaning_fee", "updated_at"])
    if Decimal(vt.cleaning_fee) <= 0:
        raise Abort(f"{vt.name} has no cleaning fee; pass --fee to set one.")
    return vt


def _contact(args, Contact):
    if args.email is None:
        raise Abort("--email is required unless --lead is given.")
    contact = Contact.objects.filter(email__iexact=args.email).order_by("pk").first()
    if contact is None:
        contact = Contact.objects.create(name=args.name, email=args.email, phone=args.phone)
        print(f"  created contact #{contact.pk} {contact.name} <{contact.email}>")
    else:
        print(f"  using contact #{contact.pk} {contact.name} <{contact.email}>")
    return contact


def _first_pickup(args, zone: ZoneInfo):
    from django.utils import timezone

    now = timezone.now().astimezone(zone)
    day = now.date() + timedelta(days=1 if args.day == "tomorrow" else 0)
    if args.time:
        clock = datetime.strptime(args.time, "%H:%M").time()
    else:
        later = now + timedelta(hours=2)
        later = later.replace(minute=(later.minute // 15) * 15, second=0, microsecond=0)
        clock = (
            later.time()
            if later.date() == now.date()
            else now.replace(hour=23, minute=30, second=0, microsecond=0).time()
        )
    return day, clock


def _make_trips(lead, args, vt, vt2, zone_name: str, Reservation, Stop) -> list:
    zone = ZoneInfo(zone_name)
    day, clock = _first_pickup(args, zone)
    stops = STOPS.get(zone_name, STOPS["America/New_York"])
    trips = []
    for i in range(max(1, min(args.trips, 4))):
        vehicle = vt2 if (i == 1 and vt2 is not None) else vt
        rate = args.rate or (Decimal(vehicle.rate) if vehicle.rate else Decimal("200"))
        pickup = datetime.combine(day + timedelta(days=i), clock)
        end = pickup + timedelta(hours=float(args.hours))
        trip = Reservation.objects.create(
            lead=lead,
            vehicle=vehicle,
            trip_type=Reservation.TripType.HOURLY,
            passengers=10,
            rate=rate,
            hours=args.hours,
            min_hours=vehicle.hourly_min_hours or 0,
            gratuity_pct=args.gratuity,
            pickup_date=pickup.date(),
            pickup_time=pickup.time(),
            pickup_timezone=zone_name,
            dropoff_date=end.date(),
            dropoff_time=end.time(),
            sort_order=i,
        )
        for seq, (name, address) in enumerate(stops):
            Stop.objects.create(reservation=trip, sequence=seq, name=name, address=address)
        trips.append(trip)
    return trips


def _attach_card(plan, card_key: str, services):
    """The card vault's shape: the contact's own Stripe customer, the test PaymentMethod
    attached to it, the card on the plan and in the vault (`services._store_card`)."""
    customer = services.get_or_create_customer(plan)
    pm = services._stripe().PaymentMethod.attach(TEST_CARDS[card_key], customer=customer)
    services._store_card(plan, pm)
    plan.refresh_from_db()
    print(f"  card on file: {plan.card_brand} ••{plan.card_last4} ({pm.id}) on customer {customer}")


def create(args) -> None:
    from django.db import transaction

    from apps.contacts.models import Contact
    from apps.core.choices import Channel
    from apps.leads.models import Lead, VehicleType
    from apps.leads.services import book_lead
    from apps.payments import holds, services
    from apps.reservations.models import Reservation, Stop
    from apps.tasks.models import TaskConfig

    try:
        ZoneInfo(args.zone)
    except Exception as exc:
        raise Abort(f"Unknown zone {args.zone!r}.") from exc
    if args.prepaid and (args.no_card or args.card != "visa"):
        raise Abort("--prepaid needs --card visa.")

    # A real run commits as it goes (Stripe calls can't roll back, so the DB must not either);
    # a dry run makes no Stripe call and rolls everything back.
    with transaction.atomic() if args.dry_run else contextlib.nullcontext():
        vt = _vehicle(args.vehicle, args.fee, VehicleType)
        vt2 = _vehicle(args.vehicle2, args.fee2, VehicleType) if args.vehicle2 else None
        if args.lead:
            lead = Lead.objects.select_related("contact").filter(pk=args.lead).first()
            if lead is None:
                raise Abort(f"No lead #{args.lead}.")
            if lead.status == Lead.Status.LOST:
                raise Abort("That order is lost.")
            if lead.on_account:
                raise Abort("That order is billed to an account; it is never held.")
            changed = lead.reservations.update(vehicle=vt)
            print(f"  converting {lead.quote_no}: {changed} trip(s) put in {vt.name}")
            trips = list(lead.reservations.order_by("pickup_date", "pickup_time", "pk"))
        else:
            contact = _contact(args, Contact)
            lead = Lead.objects.create(
                contact=contact, channel=Channel.PHONE, notes="E2E cleaning-hold order"
            )
            trips = _make_trips(lead, args, vt, vt2, args.zone, Reservation, Stop)
            print(f"  created order {lead.quote_no} (lead #{lead.pk}) with {len(trips)} trip(s)")
        if lead.is_farm_in:
            print(
                "  WARNING: the contact's company is an affiliate account; farm-in is never held."
            )
        plan = services.ensure_plan(lead)
        if not args.no_card and not args.dry_run:
            _attach_card(plan, args.card, services)
        elif not args.no_card:
            print(
                f"  [dry-run] would attach {TEST_CARDS[args.card]} on the contact's Stripe customer"
            )
        book_lead(lead)
        lead.refresh_from_db()
        plan.refresh_from_db()
        if plan.quote_total == 0:
            plan.snapshot_total()
        if args.prepaid and not args.dry_run:
            charge = services.charge_saved_card(plan, plan.quote_total)
            print(f"  prepaid ${charge.amount} ({charge.status})")
        elif args.prepaid:
            print(f"  [dry-run] would charge ${plan.quote_total} to the card now")

        for t in trips:
            t.refresh_from_db()
            print(
                f"  trip #{t.pk} {t.reference} · {t.pickup_date} {t.pickup_time:%H:%M} "
                f"{t.pickup_at.tzname()} · {t.vehicle.name} (fee ${t.vehicle.cleaning_fee}) "
                f"· total ${t.line_total}"
            )
        config = TaskConfig.load()
        print(
            f"  held: {holds.eligible(lead)} · hold amount ${holds.hold_amount(lead)} · "
            f"switch {'ON' if config.hold_enabled else 'OFF'} · place time "
            f"{config.hold_place_time:%H:%M} trip-local"
        )
        print(f"  order page:  {BASE_URL}/portal/orders/{lead.pk}/")
        print(f"  workspace:   {BASE_URL}/portal/leads/{lead.pk}/")
        if args.dry_run:
            raise Rollback


def done(args) -> None:
    from django.db import transaction

    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead
    from apps.reservations.models import Reservation
    from apps.reservations.services import set_trip_status

    with transaction.atomic():
        lead = Lead.objects.filter(pk=args.lead).first()
        if lead is None:
            raise Abort(f"No lead #{args.lead}.")
        trips = lead.reservations.exclude(trip_status__in=CANCELLED_STATUSES)
        if args.trip:
            trips = trips.filter(pk=args.trip)
        trips = list(trips.order_by("pickup_date", "pickup_time", "pk"))
        if not trips:
            raise Abort("No live trip to mark Done.")
        for trip in trips:
            event = set_trip_status(trip, Reservation.TripStatus.DONE)
            print(f"  trip #{trip.pk} {trip.reference}: {'Done' if event else 'already Done'}")
        print(f"  processing:  {BASE_URL}/portal/processing/?trip={trips[0].pk}")
        if args.dry_run:
            raise Rollback


def main() -> int:
    args = _parser().parse_args()
    django_ready()
    try:
        (create if args.cmd == "create" else done)(args)
    except Rollback:
        print("Dry run: rolled back, nothing saved.")
    except Abort as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
