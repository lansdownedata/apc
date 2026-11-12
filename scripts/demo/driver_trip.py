"""Make a booked hourly trip that picks up soon, cover it, and print its driver link.

    python scripts/demo/driver_trip.py                      # covered by E2E Affiliate Limo
    python scripts/demo/driver_trip.py --in-minutes 90 --vendor "E2E Potomac Livery"

The trip is a Sprinter Van night out in DC with real coordinates on both stops (so the map can
draw), booked without a payment, then marked assigned to the vendor. That confirms coverage,
puts the trip at Dispatched and sends the vendor the "Trip confirmed" email carrying the link.
The link is printed here too. Each run makes a new order.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from _bootstrap import django_ready  # noqa: E402

ZONE = "America/New_York"
EMAIL = "moe+demo-nightout@lansdownedata.com"
STOPS = [
    ("The Jefferson", "1200 16th St NW, Washington, DC 20036", "38.905900", "-77.036700"),
    ("The Anthem", "901 Wharf St SW, Washington, DC 20024", "38.878900", "-77.025800"),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--in-minutes", type=int, default=60, help="Pickup this many minutes from now.")
    p.add_argument("--vendor", default="E2E Affiliate Limo", help="Vendor name to cover the trip.")
    p.add_argument("--passenger-mobile", default="", help="Lead passenger's mobile (optional).")
    args = p.parse_args()
    django_ready()

    from django.conf import settings
    from django.db import transaction
    from django.utils import timezone

    from apps.contacts.models import Contact
    from apps.core.choices import Channel
    from apps.dispatch import services as dispatch
    from apps.leads.models import Lead, ServiceType, VehicleType
    from apps.leads.services import book_lead
    from apps.reservations.acknowledgements import driver_link_url
    from apps.reservations.models import Passenger, Reservation, Stop
    from apps.vendors.models import Vendor

    vendor = Vendor.objects.filter(name__iexact=args.vendor).first()
    if vendor is None:
        sys.exit(f"No vendor named {args.vendor!r}.")
    van = VehicleType.objects.get(name="Sprinter Van")
    service = ServiceType.objects.get(name="Night Out")

    with transaction.atomic():
        contact = Contact.objects.filter(email__iexact=EMAIL).first()
        if contact is None:
            contact = Contact.objects.create(
                name="Jordan Ellis", email=EMAIL, channel=Channel.PHONE
            )
        lead = Lead.objects.create(contact=contact, channel=Channel.PHONE, notes="Demo: night out")
        pickup = timezone.now().astimezone(ZoneInfo(ZONE)) + timedelta(minutes=args.in_minutes)
        pickup = pickup.replace(second=0, microsecond=0, tzinfo=None)
        hours = van.hourly_min_hours or Decimal("4")
        end = pickup + timedelta(hours=float(hours))
        trip = Reservation.objects.create(
            lead=lead,
            vehicle=van,
            trip_type=Reservation.TripType.HOURLY,
            service_type=service,
            passengers=8,
            rate=van.rate,
            min_hours=van.hourly_min_hours,
            gratuity_pct=Decimal("20"),
            pickup_date=pickup.date(),
            pickup_time=pickup.time(),
            pickup_timezone=ZONE,
            dropoff_date=end.date(),
            dropoff_time=end.time(),
        )
        for seq, (name, address, lat, lon) in enumerate(STOPS):
            Stop.objects.create(
                reservation=trip,
                sequence=seq,
                name=name,
                address=address,
                latitude=Decimal(lat),
                longitude=Decimal(lon),
            )
        Passenger.objects.create(
            reservation=trip, name="Jordan Ellis", mobile=args.passenger_mobile, is_lead=True
        )
        book_lead(lead)
        trip.refresh_from_db()
        payout = (Decimal(trip.line_total) * Decimal("0.65")).quantize(Decimal("0.01"))
        assignment = dispatch.assign_direct(trip, vendor, payout=payout, note="Demo coverage")

    trip.refresh_from_db()
    print(f"order {lead.quote_no} · trip {trip.reference} · {trip.get_trip_status_display()}")
    print(f"pickup {pickup:%a %b %-d, %-I:%M %p} ET · covered by {vendor.name} <{vendor.email}>")
    print(f"driver link: {driver_link_url(assignment, base_url=settings.PUBLIC_BASE_URL or '')}")
    print(f"board:       {settings.PUBLIC_BASE_URL}/portal/dispatch/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
