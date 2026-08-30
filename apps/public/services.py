"""Public marketing-site orchestration: turn a validated booking request into a Lead."""

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.reservations.flights import link_flights
from apps.reservations.models import Reservation, Stop


def create_lead_from_booking(data: dict) -> Lead:
    """Turn a validated public booking request into a NEW Lead + reservation stub."""
    contact = Contact.objects.match_or_create(
        name=data["name"],
        phone=data.get("phone", ""),
        email=data.get("email", ""),
        channel=Channel.WEBSITE,
    )
    lead = Lead.objects.create(
        contact=contact,
        status=Lead.Status.NEW,
        channel=Channel.WEBSITE,
        notes=data.get("notes", ""),
    )
    reservation = Reservation.objects.create(
        lead=lead,
        trip_type=data.get("trip_type") or Reservation.TripType.TRANSFER,
        hours=data.get("hours") or 0,
        service=data.get("service", ""),
        pickup_date=data.get("pickup_date"),
        pickup_time=data.get("pickup_time"),
        passengers=data.get("passengers") or 1,
    )
    stops = data.get("stops") or []
    last = len(stops) - 1
    for i, s in enumerate(stops):
        # Same positional rule as reservations.drafts: the ends are fixed, a middle stop
        # keeps what the visitor chose (or blank), and no airport means no direction.
        if not s.get("airport_id"):
            s["flight_direction"] = ""
        elif i == 0:
            s["flight_direction"] = "arrival"
        elif i == last:
            s["flight_direction"] = "departure"
        else:
            s.setdefault("flight_direction", "")
    link_flights(stops, reservation.pickup_date)
    if stops:
        Stop.objects.bulk_create(
            [
                Stop(
                    reservation=reservation,
                    sequence=i,
                    address=s.get("address", ""),
                    latitude=s.get("lat"),
                    longitude=s.get("lng"),
                    airport_id=s.get("airport_id"),
                    airline_id=s.get("airline_id"),
                    flight_number=s.get("flight_number", ""),
                    flight_direction=s.get("flight_direction", ""),
                    flight_id=s.get("flight_id"),
                )
                for i, s in enumerate(stops)
            ]
        )
    Notification.notify(
        lead,
        Notification.Kind.NEW_LEAD,
        title=f"New website booking: {contact.name}",
        detail=data.get("service", ""),
    )
    return lead
