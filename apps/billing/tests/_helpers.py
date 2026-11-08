"""Shared builders for the billing tests (APC-106)."""

from datetime import time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation


def _processed_trip(group=None, *, accountant, **kw) -> Reservation:
    """A finished trip of a booked order billed to `group` (a new one if omitted), already
    processed to "invoice": exactly what Customer Billing offers for invoicing.
    `pickup_date` and `contact` may be overridden."""
    lead = LeadFactory(
        status=Lead.Status.BOOKED, contact=kw.pop("contact", None) or ContactFactory()
    )
    lead.billing_group = group or AccountGroupFactory(account__contact=lead.contact)
    lead.save(update_fields=["billing_group"])
    trip = ReservationFactory(
        lead=lead,
        trip_status=Reservation.TripStatus.DONE,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=kw.pop("pickup_date", timezone.localdate() - timedelta(days=1)),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
        **kw,
    )
    trip.booked_total = trip.line_total
    trip.save(update_fields=["booked_total"])
    processing.process(trip, user=accountant, expected=str(trip.line_total), collect="invoice")
    return trip
