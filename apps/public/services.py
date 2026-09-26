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

from .wedding import build_notes, is_time_sensitive, wedding_answers


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
        reservation.refresh_pickup_timezone()
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
    differently-cased row rather than reusing it. One catalog for the website and the
    office is the whole point of `ServiceType` — a second wedding row is exactly the
    drift it exists to stop.
    """
    existing = ServiceType.objects.filter(name__iexact=WEDDING_SERVICE_NAME).first()
    return existing or ServiceType.objects.create(name=WEDDING_SERVICE_NAME)


def create_lead_from_wedding(data: dict, *, lead: Lead | None = None) -> Lead:
    """One wedding → one Lead holding every answer and NO trips.

    Same Contact matching, Channel and Notification as `create_lead_from_booking`; the
    difference is that nothing is derived from the answers. Weddings run too many
    different ways for a rule to guess the legs, so the office builds the trips in the
    workspace from the details kept here.

    Pass `lead` to update an existing one in place (the emailed resume link) rather than
    leaving the office holding two versions of the same wedding. An update refreshes the
    details and nothing else: by then the lead may carry trips an agent built and notes an
    agent wrote, and neither is the customer's to overwrite.
    """
    if lead is not None:
        update_wedding_details(lead, data)
        venue_name = lead.intake_payload["venue_name"] or "venue TBD"
        Notification.notify(
            lead,
            Notification.Kind.NEW_LEAD,
            title=f"Wedding details updated: {lead.contact.name}",
            detail=venue_name,
        )
        return lead

    payload = wedding_payload(data)
    venue_name = payload["venue_name"] or "venue TBD"
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
        notes=build_notes(payload),
        has_alert=is_time_sensitive(data["wedding_date"], timezone.localdate()),
        intake_payload=payload,
    )
    Notification.notify(
        lead,
        Notification.Kind.NEW_LEAD,
        title=f"New wedding request: {contact.name}",
        detail=f"{venue_name} · details ready to build",
    )
    return lead


def update_wedding_details(lead: Lead, data: dict) -> Lead:
    """Replace a wedding's answers — and nothing else — from a validated form.

    The one write path behind both the customer's resume link and the office's Edit
    details. It never touches the trips (an agent builds those by hand) or `Lead.notes`
    (an agent's to edit), and it keeps the contact the lead already has: the office form
    carries no contact fields, and a customer updating a guest count is not renaming
    themselves in the CRM.
    """
    contact = lead.contact
    lead.intake_payload = wedding_payload(
        {**data, "name": contact.name, "email": contact.email, "phone": contact.phone}
    )
    lead.has_alert = is_time_sensitive(data["wedding_date"], timezone.localdate())
    lead.save(update_fields=["has_alert", "intake_payload", "updated_at"])
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
    """The answers, JSON-safe, exactly as the intake collected them.

    The lead's record of the wedding: `wedding_answers` reads it back by category, and
    the resume link rehydrates the form from it. Every hotel keeps its own address and
    coordinates, which is what the office needs to build pickups from.
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
        "notes": (data.get("notes") or "").strip(),
    }


def send_wedding_confirmation(lead: Lead, *, base_url: str) -> bool:
    """Email the couple every detail they gave us and the link back to them. Best-effort.

    Silently skipped without an email address (phone-only is a normal answer) or
    without PUBLIC_BASE_URL, since a relative resume link in an inbox is useless.
    """
    email = (lead.contact.email or "").strip()
    if not email or not base_url:
        return False
    resume_url = (
        f"{base_url.rstrip('/')}{reverse('public:wedding_resume', args=[make_wedding_token(lead)])}"
    )
    return send_html_email(
        to=email,
        subject=f"We have your wedding details · {lead.quote_no}",
        template="wedding_request",
        context={
            "lead": lead,
            "contact": lead.contact,
            "answers": wedding_answers(lead.intake_payload),
            "resume_url": resume_url,
            "company_name": settings.COMPANY_NAME,
            "company_phone": settings.COMPANY_PHONE,
            "company_email": settings.COMPANY_EMAIL,
        },
    )


_BOOKING_SALT = "public.schedule.booking"
# Only has to outlive the redirect from the booking POST to the thanks page.
BOOKING_TOKEN_MAX_AGE_SECONDS = 60 * 60


def make_booking_token(*, name: str, start_time: str, timezone: str) -> str:
    """A signed token for the discovery-call thanks page.

    Carries the three things the confirmation has to say and nothing more. The token
    lands in a URL — which ends up in history, referrer headers and access logs — so
    the invitee's email and phone stay out of it.

    The timezone rides along because the confirmation must agree with the slot the
    visitor clicked and with the calendar invite Calendly emails them, both of which
    are in THEIR zone. This is the one page on the site that does not render in
    TIME_ZONE: showing 1:45 PM EDT to someone who booked 10:45 AM PDT reads as a
    different appointment entirely.
    """
    return signing.dumps(
        {"name": name, "start_time": start_time, "timezone": timezone}, salt=_BOOKING_SALT
    )


def read_booking_token(token: str) -> dict:
    """The payload behind a booking token. Raises BadSignature or SignatureExpired."""
    return signing.loads(token, salt=_BOOKING_SALT, max_age=BOOKING_TOKEN_MAX_AGE_SECONDS)
