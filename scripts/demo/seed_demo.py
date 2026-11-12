"""Seed the client-demo records on the local rig: a corporate customer on terms with processed
trips waiting on Customer Billing.

    python scripts/demo/seed_demo.py --dry-run
    python scripts/demo/seed_demo.py

Creates (once; a second run changes nothing):

- Company "Meridian Partners" and its booker, Dana Whitfield.
- A billing account "Meridian Partners" (Net 30) with the group "Executive Travel", PO required.
- One booked order billed to that group with two past trips: an airport transfer from IAD and
  an hourly corporate trip. Both are set Done and processed through the real processing
  service with collect="invoice", so they show on /portal/billing/ ready to invoice.

No card is touched and nothing is sent to a customer. QuickBooks sees the account on the next
retry-qbo-sync run, and the invoice once you press Create invoice.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from _bootstrap import BASE_URL, django_ready  # noqa: E402

ZONE = "America/New_York"
EMAIL = "moe+meridian@lansdownedata.com"
ACCOUNT = "Meridian Partners"
GROUP = "Executive Travel"
PO = "PO-4471"

JEFFERSON = ("The Jefferson", "1200 16th St NW, Washington, DC 20036", "38.905900", "-77.036700")
MERIDIAN_HQ = (
    "Meridian Partners",
    "1750 Tysons Blvd, McLean, VA 22102",
    "38.923500",
    "-77.222600",
)


class Rollback(Exception):
    pass


def _stop(Stop, trip, seq: int, place: tuple[str, str, str, str], **extra) -> None:
    name, address, lat, lon = place
    Stop.objects.create(
        reservation=trip,
        sequence=seq,
        name=name,
        address=address,
        latitude=Decimal(lat),
        longitude=Decimal(lon),
        **extra,
    )


def seed(user_email: str) -> None:
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.addresses.models import Airport
    from apps.billing import services as billing
    from apps.billing.models import BillingAccount
    from apps.contacts.models import Company, Contact
    from apps.core.choices import Channel
    from apps.leads.models import Lead, ServiceType, VehicleType
    from apps.leads.services import book_lead
    from apps.payments import trip_money
    from apps.reservations import processing
    from apps.reservations.models import Passenger, Reservation, Stop
    from apps.reservations.services import set_trip_status

    user = get_user_model().objects.filter(email__iexact=user_email).first()
    if user is None:
        sys.exit(f"No user with email {user_email!r}.")
    if BillingAccount.objects.filter(name=ACCOUNT).exists():
        account = BillingAccount.objects.get(name=ACCOUNT)
        print(f"Already seeded: account #{account.pk} {ACCOUNT}. Nothing changed.")
        print(f"  {BASE_URL}/portal/billing/?group={account.default_group.pk}")
        return

    suv = VehicleType.objects.get(name="Luxury SUV")
    sedan = VehicleType.objects.get(name="Luxury Sedan")
    airport_service = ServiceType.objects.get(name="Airport Transfer")
    corporate_service = ServiceType.objects.get(name="Corporate Travel")
    iad = Airport.objects.get(iata="IAD")

    company, _ = Company.objects.get_or_create(name=ACCOUNT)
    contact = Contact.objects.filter(email__iexact=EMAIL).first()
    if contact is None:
        contact = Contact.objects.create(
            name="Dana Whitfield", email=EMAIL, company=company, channel=Channel.PHONE
        )
    print(f"  contact #{contact.pk} {contact.name} at {company.name}")

    account = billing.create_account(
        contact,
        name=ACCOUNT,
        group_name=GROUP,
        invoice_email="moe+meridian-ap@lansdownedata.com",
        po_required=True,
    )
    group = account.default_group
    print(f"  billing account #{account.pk} {account.name} · group #{group.pk} {group.name}")

    lead = Lead.objects.create(
        contact=contact, channel=Channel.PHONE, notes="Demo: executive travel on account"
    )
    now = timezone.now().astimezone(ZoneInfo(ZONE))

    # Trip 1: yesterday's airport transfer, flat rate.
    pickup = datetime.combine((now - timedelta(days=1)).date(), time(16, 30))
    end = pickup + timedelta(hours=1)
    transfer = Reservation.objects.create(
        lead=lead,
        vehicle=suv,
        trip_type=Reservation.TripType.TRANSFER,
        service_type=airport_service,
        passengers=2,
        rate=suv.rate,
        min_hours=suv.transfer_min_hours,
        gratuity_pct=Decimal("20"),
        pickup_date=pickup.date(),
        pickup_time=pickup.time(),
        pickup_timezone=ZONE,
        dropoff_date=end.date(),
        dropoff_time=end.time(),
        sort_order=0,
    )
    _stop(
        Stop,
        transfer,
        0,
        (iad.name, "1 Saarinen Cir, Dulles, VA 20166", str(iad.latitude), str(iad.longitude)),
        airport=iad,
    )
    _stop(Stop, transfer, 1, JEFFERSON)

    # Trip 2: an hourly corporate day two days ago, billed at the sedan's minimum.
    pickup2 = datetime.combine((now - timedelta(days=2)).date(), time(9, 0))
    end2 = pickup2 + timedelta(hours=float(sedan.hourly_min_hours or 3))
    hourly = Reservation.objects.create(
        lead=lead,
        vehicle=sedan,
        trip_type=Reservation.TripType.HOURLY,
        service_type=corporate_service,
        passengers=1,
        rate=sedan.rate,
        min_hours=sedan.hourly_min_hours,
        gratuity_pct=Decimal("20"),
        pickup_date=pickup2.date(),
        pickup_time=pickup2.time(),
        pickup_timezone=ZONE,
        dropoff_date=end2.date(),
        dropoff_time=end2.time(),
        sort_order=1,
    )
    _stop(Stop, hourly, 0, JEFFERSON)
    _stop(Stop, hourly, 1, MERIDIAN_HQ)

    for trip in (transfer, hourly):
        Passenger.objects.create(reservation=trip, name="Dana Whitfield", is_lead=True)

    billing.bill_to_account(lead, group, po_number=PO, user=user)
    lead.refresh_from_db()
    book_lead(lead)
    print(f"  order {lead.quote_no} (lead #{lead.pk}) booked, billed to {GROUP}, {PO}")

    for trip in (transfer, hourly):
        trip.refresh_from_db()
        set_trip_status(trip, Reservation.TripStatus.DONE, user=user)
        trip.refresh_from_db()
        money = trip_money.money_for(trip)
        processing.process(
            trip,
            user=user,
            expected=money.difference,
            collect="invoice" if money.difference > 0 else "",
        )
        print(f"  trip {trip.reference} processed · ${trip.line_total:,.2f} waiting on an invoice")

    print(f"  contact:  {BASE_URL}/portal/contacts/{contact.pk}/")
    print(f"  order:    {BASE_URL}/portal/orders/{lead.pk}/")
    print(f"  billing:  {BASE_URL}/portal/billing/?group={group.pk}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--user", default="moe@lansdownedata.com", help="Staff user recorded.")
    p.add_argument("--dry-run", action="store_true", help="Do it all, print, then roll back.")
    args = p.parse_args()
    django_ready()
    from django.db import transaction

    try:
        with transaction.atomic():
            seed(args.user)
            if args.dry_run:
                raise Rollback
    except Rollback:
        print("Dry run: rolled back, nothing saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
