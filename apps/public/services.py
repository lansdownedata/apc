"""Public marketing-site orchestration: turn a validated request into a Lead."""

from dataclasses import asdict

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.choices import Channel
from apps.leads.models import Lead, ServiceType
from apps.notifications.email import send_html_email
from apps.notifications.models import Notification
from apps.reservations.flights import link_flights
from apps.reservations.models import Reservation, Stop

from .wedding import Site, build_notes, hotel_label, is_time_sensitive


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
        service_type=data.get("service_type"),
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
        detail=service_type.name if (service_type := data.get("service_type")) else "",
    )
    return lead


WEDDING_SERVICE_NAME = "Wedding Transportation"


def wedding_service_type() -> ServiceType:
    """The Settings catalog's wedding occasion, created only if it has been deleted.

    Looked up case-insensitively because `ServiceType` carries a `Lower(name)` unique
    constraint — a plain `get_or_create(name=...)` would raise IntegrityError against a
    differently-cased row rather than reusing it. The spec called this occasion
    "Wedding"; the catalog seeded by leads.0008 already calls it "Wedding
    Transportation", and one catalog for the website and the office is the whole point
    of `ServiceType` — a second wedding row is exactly the drift it exists to stop.
    """
    existing = ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).first()
    return existing or ServiceType.objects.create(name=WEDDING_SERVICE_NAME)


def wedding_sites(data: dict) -> dict[str, Site]:
    """Every place this wedding touches, keyed by the name the legs refer to it by.

    The legs arrive from the browser carrying names only; coordinates and street lines
    are looked up here from the venues the *form* resolved, never taken from the client.
    """
    sites: dict[str, Site] = {}
    for site in [data.get("venue"), data.get("ceremony"), *(data.get("hotels") or [])]:
        if site is not None:
            sites.setdefault(site.name, site)
    # The composite "2 hotels — …" origin is a derived label, not a place; it still needs
    # to resolve so its stop carries a name rather than an empty address.
    label = hotel_label(data.get("hotels") or [], bool(data.get("hotels_tbd")))
    sites.setdefault(label, Site(name=label))
    return sites


def wedding_stop(reservation: Reservation, sequence: int, name: str, sub: str, sites: dict) -> Stop:
    site = sites.get(name)
    return Stop(
        reservation=reservation,
        sequence=sequence,
        name=name[:160],
        address=((site.line if site else "") or sub)[:255],
        latitude=site.latitude if site else None,
        longitude=site.longitude if site else None,
    )


def create_lead_from_wedding(data: dict, *, lead: Lead | None = None) -> Lead:
    """One wedding → one Lead holding one Reservation per confirmed leg.

    Deliberately parallel to `create_lead_from_booking`: same Contact matching, same
    Channel, same Notification. The only difference is that a wedding fans out into
    several reservations instead of one, which is what Lead → Reservation already
    models — a wedding is not a special case, it is the general case used properly.

    Pass `lead` to rebuild an existing one in place (the resume link, spec §7.4) rather
    than leaving the office holding two versions of the same wedding.
    """
    legs = data["legs"]
    contact = Contact.objects.match_or_create(
        name=data["name"],
        phone=data.get("phone", ""),
        email=data.get("email", ""),
        channel=Channel.WEBSITE,
    )
    notes = build_notes(
        wedding_date=data["wedding_date"],
        venue=data.get("venue"),
        ceremony=data.get("ceremony"),
        hotels=data.get("hotels") or [],
        hotels_tbd=bool(data.get("hotels_tbd")),
        groups=data["groups"],
        times_tbd=bool(data.get("times_tbd")),
        legs=legs,
    )
    has_alert = is_time_sensitive(data["wedding_date"], timezone.localdate())
    payload = wedding_payload(data)
    if lead is None:
        lead = Lead.objects.create(
            contact=contact,
            status=Lead.Status.NEW,
            channel=Channel.WEBSITE,
            notes=notes,
            has_alert=has_alert,
            intake_payload=payload,
        )
    else:
        lead.contact = contact
        lead.notes = notes
        lead.has_alert = has_alert
        lead.intake_payload = payload
        lead.save(update_fields=["contact", "notes", "has_alert", "intake_payload", "updated_at"])
        lead.reservations.all().delete()

    service_type = wedding_service_type()
    sites = wedding_sites(data)
    stops = []
    for i, leg in enumerate(legs):
        reservation = Reservation.objects.create(
            lead=lead,
            sort_order=i,
            trip_type=Reservation.TripType.TRANSFER,
            service_type=service_type,
            pickup_date=data["wedding_date"],
            pickup_time=leg["time"],
            passengers=leg["pax"],
        )
        stops.append(wedding_stop(reservation, 0, leg["from"], leg.get("from_sub", ""), sites))
        stops.append(wedding_stop(reservation, 1, leg["to"], leg.get("to_sub", ""), sites))
    Stop.objects.bulk_create(stops)

    venue_name = data["venue"].name if data.get("venue") else "venue TBD"
    Notification.notify(
        lead,
        Notification.Kind.NEW_LEAD,
        title=f"New wedding request: {contact.name}",
        detail=f"{len(legs)} movement{'' if len(legs) == 1 else 's'} · {venue_name}",
    )
    return lead


_WEDDING_SALT = "public.wedding.resume"
# A couple 12 months out will come back when their hotel block is set; without a link
# that still works then, they start over or they don't return (spec §7.4).
WEDDING_TOKEN_MAX_AGE_SECONDS = 180 * 24 * 60 * 60


def make_wedding_token(lead: Lead) -> str:
    """An opaque signed token for the thanks page and the emailed resume link.

    Carries the lead id and nothing else: the URL ends up in an inbox, so no name,
    email or venue may ride in it.
    """
    return signing.dumps({"lead": lead.pk}, salt=_WEDDING_SALT)


def read_wedding_token(token: str) -> Lead:
    """The Lead behind a signed token. Raises BadSignature or Lead.DoesNotExist."""
    data = signing.loads(token, salt=_WEDDING_SALT, max_age=WEDDING_TOKEN_MAX_AGE_SECONDS)
    return Lead.objects.get(pk=data["lead"])


def wedding_payload(data: dict) -> dict:
    """The answers, JSON-safe, exactly as the seven steps collected them.

    Stored on the lead so the resume link rehydrates the form rather than only the
    itinerary — "come back when the hotel block is set" has to land them where they
    left off, not at step one.
    """
    return {
        "name": data.get("name", ""),
        "email": data.get("email", ""),
        "phone": data.get("phone", ""),
        "wedding_date": data["wedding_date"].isoformat(),
        "venue": asdict(data["venue"]) if data.get("venue") else None,
        "venue_name": data["venue"].name if data.get("venue") else "",
        "ceremony": asdict(data["ceremony"]) if data.get("ceremony") else None,
        "same_site": bool(data.get("same_site")),
        "groups": list(data.get("groups") or []),
        "guest_count": data.get("guest_count"),
        "party_count": data.get("party_count"),
        "family_count": data.get("family_count"),
        "hotels": [asdict(h) for h in data.get("hotels") or []],
        "hotels_tbd": bool(data.get("hotels_tbd")),
        "ceremony_time": data["ceremony_time"].strftime("%H:%M")
        if data.get("ceremony_time")
        else "",
        "end_time": data["end_time"].strftime("%H:%M") if data.get("end_time") else "",
        "times_tbd": bool(data.get("times_tbd")),
        "legs": [{**leg, "time": leg["time"].strftime("%H:%M")} for leg in data.get("legs") or []],
    }


def send_wedding_confirmation(lead: Lead, *, base_url: str) -> bool:
    """Email the couple their itinerary and the link back into it. Best-effort.

    Silently skipped without an email address (phone-only is a normal answer) or
    without PUBLIC_BASE_URL, since a relative resume link in an inbox is useless.
    """
    email = (lead.contact.email or "").strip()
    if not email or not base_url:
        return False
    resume_url = (
        f"{base_url.rstrip('/')}{reverse('public:wedding_resume', args=[make_wedding_token(lead)])}"
    )
    reservations = lead.reservations.prefetch_related("stops").order_by("sort_order", "id")
    return send_html_email(
        to=email,
        subject=f"Your wedding transportation plan · {lead.quote_no}",
        template="wedding_request",
        context={
            "lead": lead,
            "contact": lead.contact,
            "reservations": reservations,
            "resume_url": resume_url,
            "company_name": settings.COMPANY_NAME,
            "company_phone": settings.COMPANY_PHONE,
            "company_email": settings.COMPANY_EMAIL,
        },
    )
