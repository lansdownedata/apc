"""Office reports over reservation state (APC-19)."""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .models import Reservation


def unconfirmed_trip_rows(today: date | None = None) -> list[dict]:
    """Tomorrow's trips the customer has never acknowledged, one row per customer.

    The T-72h and T-48h notices are the automated asks (`messaging.touchpoints`); this is
    the fallback the client asked for — at T-24h an unconfirmed day stops chasing itself
    and goes to the office to be confirmed by hand.
    """
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.leads.models import Lead

    today = today or timezone.localdate()
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    trips = (
        Reservation.objects.filter(
            lead__status=Lead.Status.BOOKED,
            pickup_date=today + timedelta(days=1),
            customer_confirmed_at__isnull=True,
        )
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .select_related("lead", "lead__contact", "vehicle", "service_type")
        .prefetch_related("stops")
        .order_by("lead__contact__name", "pickup_time", "pk")
    )

    rows: dict[int, dict] = {}
    for trip in trips:
        contact = trip.lead.contact
        row = rows.get(contact.pk)
        if row is None:
            row = rows[contact.pk] = {
                "contact": contact,
                "customer": contact.name,
                "email": contact.email or "",
                "phone": contact.phone or "",
                "trips": [],
                # The deep link is per order — a customer's trips can span several.
                "url": f"{base}{reverse('lead_detail', args=[trip.lead_id])}",
            }
        row["trips"].append(_trip_row(trip))
    return list(rows.values())


def _trip_row(trip: Reservation) -> dict:
    """One trip line, pre-rendered off the prefetched stops.

    Deliberately not `trip.pickup` / `trip.dropoff`: those are properties over
    `stops.order_by(...)`, which bypasses the prefetch and costs two queries per trip.
    """
    stops = sorted(trip.stops.all(), key=lambda s: s.sequence)
    addresses = [s.address for s in stops if s.address]
    if trip.pickup_time is not None:
        when = f"{trip.pickup_time:%-I:%M %p} {trip.pickup_tz_abbrev}".strip()
    else:
        when = "time TBD"
    return {
        "reservation": trip,
        "quote_no": trip.lead.quote_no,
        "when": when,
        "route": " → ".join(addresses) if addresses else "route TBD",
    }
