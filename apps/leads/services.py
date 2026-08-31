"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.integrations import podium
from apps.messaging import touchpoints
from apps.notifications.email import send_html_email
from apps.payments.models import PaymentPlan
from apps.public.wedding import MAX_COACH_SEATS

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .models import Lead, VehicleType

_DEPOSIT_SALT = "quote-deposit"


def agent_options() -> list[tuple[int, str]]:
    """`(pk, display name)` for every user, for the assign-to pickers."""
    return [
        (u.pk, u.get_full_name() or u.username)
        for u in User.objects.order_by("first_name", "username")
    ]


def service_type_options(lead: Lead | None = None) -> list[tuple[int, str]]:
    """`(pk, name)` for the reservation editor's Service picker.

    Active catalog entries, plus any retired one this lead's trips already use. Without
    that second half, opening a legacy trip whose type has since been deactivated would
    show an empty picker — and Tom Select only displays values that are registered
    options — so saving would silently blank the service the trip had.
    """
    from .models import ServiceType

    active = ServiceType.objects.filter(active=True)
    if lead is not None:
        in_use = ServiceType.objects.filter(reservation__lead=lead)
        active = ServiceType.objects.filter(pk__in=active.union(in_use).values("pk"))
    return list(active.values_list("pk", "name"))


def make_deposit_token(lead: Lead) -> str:
    """An opaque, signed token encoding the lead id for the public deposit pages."""
    return signing.dumps({"lead": lead.pk}, salt=_DEPOSIT_SALT)


def read_deposit_token(token: str) -> Lead:
    """Return the Lead for a signed token. Raises BadSignature if forged/tampered."""
    from .models import Lead

    data = signing.loads(token, salt=_DEPOSIT_SALT)
    return Lead.objects.get(pk=data["lead"])


def compute_quote_expiry(lead: Lead) -> datetime:
    """Client rule: 14 days before first pickup; late lead => the pickup itself;
    no pickup date => 14 days from now."""
    days = settings.QUOTE_EXPIRY_DAYS_BEFORE_PICKUP
    first = (
        lead.reservations.filter(pickup_date__isnull=False)
        .order_by("pickup_date", "pickup_time")
        .first()
    )
    if first is None:
        return timezone.now() + timedelta(days=days)
    pickup = first.pickup_at
    cutoff = pickup - timedelta(days=days)
    return cutoff if cutoff > timezone.now() else pickup


def make_quote_page_url(lead: Lead, *, base_url: str) -> str:
    return f"{base_url}{reverse('quote_page', args=[make_deposit_token(lead)])}"


def make_pay_page_url(lead: Lead, *, base_url: str) -> str:
    return f"{base_url}{reverse('quote_pay', args=[make_deposit_token(lead)])}"


@dataclass
class SendQuoteResult:
    ok: bool
    http_status: int = 200
    error: str = ""
    link: str = ""
    status: str = ""
    delivery: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "link": self.link,
            "status": self.status,
            "delivery": self.delivery,
        }


def _quote_message(lead: Lead, plan: PaymentPlan, link: str) -> str:
    contact = lead.contact
    return (
        f"Hi {contact.name}, here's your All Pro Charter quote {lead.quote_no} "
        f"for ${plan.quote_total:,.2f}. To confirm, pay your {plan.deposit_pct}% "
        f"deposit of ${plan.deposit_amount:,.2f} here: {link}"
    )


def _quote_email_context(lead: Lead, plan: PaymentPlan, link: str) -> dict:
    """Template context for templates/email/quote_sent.{html,txt}."""
    return {
        "contact_name": lead.contact.name,
        "quote_no": lead.quote_no,
        "quote_total": f"{plan.quote_total:,.2f}",
        "deposit_pct": plan.deposit_pct,
        "deposit_amount": f"{plan.deposit_amount:,.2f}",
        "quote_url": link,
        "trip_count": lead.reservations.count(),
        "expires_at": lead.quote_expires_at,
        "company_name": settings.COMPANY_NAME,
        "company_phone": settings.COMPANY_PHONE,
        "company_email": settings.COMPANY_EMAIL,
        # The banner logo is embedded as an inline CID attachment (see _quote_logo /
        # the send_html_email call) so it renders without a remote fetch.
        "logo_cid": "logo" if _quote_logo() else "",
    }


def _quote_logo() -> str | None:
    """Absolute path to the email banner logo PNG (email clients can't render the SVG),
    or None if it isn't collectable. Attached inline as cid:logo."""
    return finders.find("brand/apc-logo-email.png")


def send_quote(lead: Lead, *, base_url: str, channels: set[str] | None = None) -> SendQuoteResult:
    """Create/refresh the deposit plan, transition the lead, stamp the send/expiry, and
    deliver the public quote-page link on the selected channels.

    ``channels`` is any non-empty subset of {"email", "sms"}; ``None`` (the default) means
    both. Email goes out as the branded HTML/text pair via
    ``apps.notifications.email.send_html_email``; SMS keeps the short Podium text message.
    Delivery is per-channel best-effort — the NEW->QUOTED transition, the
    quote_sent_at/quote_expires_at stamps, the quote_viewed_at reset, the PaymentPlan
    snapshot, and touch-point scheduling all commit even if every channel fails to send, so a
    missing Podium scope or a broken mail relay degrades gracefully. The Stripe deposit
    Checkout itself happens later, on the quote page.
    """
    selected = channels or {"email", "sms"}

    # 1. preconditions — nothing is written on failure
    if lead.status == lead.Status.LOST:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="This quote is already lost.",
        )
    existing_plan = getattr(lead, "payment", None)
    if (
        lead.status == lead.Status.BOOKED
        and existing_plan is not None
        and existing_plan.is_paid_in_full
    ):
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="This quote is already booked.",
        )
    if lead.quote_total <= 0:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add at least one reservation before sending the quote.",
        )
    email = (lead.contact.email or "").strip()
    phone = (lead.contact.phone or "").strip()
    if "email" in selected and not email:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add a customer email before sending the quote by email.",
        )
    if "sms" in selected and not phone:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add a customer phone number before sending the quote by text.",
        )

    # 2. plan + frozen total
    plan, _ = PaymentPlan.objects.get_or_create(lead=lead)
    plan.snapshot_total()

    # 3. quote-page link (no Stripe call here — that happens at book-time)
    link = make_quote_page_url(lead, base_url=base_url)

    # 4. transition NEW -> QUOTED (idempotent; a QUOTED re-send stays QUOTED)
    if lead.status == lead.Status.NEW:
        lead.status = lead.Status.QUOTED
        lead.save(update_fields=["status", "updated_at"])

    # 5. stamp the send + expiry, reset the viewed flag, and (re)schedule touch-points.
    # Already-booked unpaid resends skip the quote-nurture program — those TPs assume Quoted.
    lead.quote_sent_at = timezone.now()
    lead.quote_expires_at = compute_quote_expiry(lead)
    lead.quote_viewed_at = None
    lead.save(update_fields=["quote_sent_at", "quote_expires_at", "quote_viewed_at", "updated_at"])
    if lead.status != lead.Status.BOOKED:
        touchpoints.schedule_quote_sent(lead)

    # 6. deliver on each selected channel — best-effort, never rolls back the transition
    delivery: dict = {}
    if "email" in selected:
        result = {"sent": False, "recipient": email, "error": None}
        try:
            logo = _quote_logo()
            sent = send_html_email(
                to=email,
                subject=f"Your {settings.COMPANY_NAME} quote {lead.quote_no}",
                template="quote_sent",
                context=_quote_email_context(lead, plan, link),
                inline_images={"logo": logo} if logo else None,
            )
            result["sent"] = sent
            if not sent:
                result["error"] = "Email delivery failed — see the server log."
        except Exception as exc:  # noqa: BLE001 — delivery must never break the send
            result["error"] = str(exc)
        delivery["email"] = result
    if "sms" in selected:
        result = {"sent": False, "recipient": phone, "error": None}
        try:
            podium.send_message(
                identifier=phone, channel_type="phone", body=_quote_message(lead, plan, link)
            )
            result["sent"] = True
        except Exception as exc:  # noqa: BLE001 — delivery must never break the send
            result["error"] = str(exc)
        delivery["sms"] = result

    return SendQuoteResult(
        ok=True, http_status=200, link=link, status=lead.status, delivery=delivery
    )


class BookLeadError(Exception):
    """Raised when a lead cannot be converted to Booked (e.g. it is Lost)."""


def book_lead(lead: Lead) -> Lead:
    """Convert a new or quoted lead to Booked without recording a payment.

    Same side-effects as the Stripe deposit webhook minus the charge: status,
    pending touch-points, a PaymentPlan snapshot if missing, and a best-effort
    LimoAnywhere / Zapier push. Idempotent when already Booked. Lost leads refuse.
    A lead with no trips is refused by the manual endpoint; the deposit webhook and
    staff card charge book regardless, since money has arrived.
    """
    from apps.integrations import la_sync

    if lead.status == lead.Status.LOST:
        raise BookLeadError("Lost leads cannot be booked.")

    already_booked = lead.status == lead.Status.BOOKED
    if not already_booked:
        lead.status = lead.Status.BOOKED
        lead.save(update_fields=["status", "updated_at"])
        touchpoints.cancel_pending(lead)

    plan, created = PaymentPlan.objects.get_or_create(lead=lead)
    if created or plan.quote_total == 0:
        plan.snapshot_total()

    if already_booked:
        return lead

    try:
        la_sync.push_lead_bookings(lead)
    except Exception:
        logger.exception("LimoAnywhere push failed for lead %s", lead.pk)
    return lead


def suggest_vehicle(passengers: int, cap: int | None = None) -> VehicleType | None:
    """The smallest active vehicle that seats one run of this size.

    Matched on capacity, never on the name `wedding.vehicle_for()` produces: those strings
    are customer-facing copy ("Executive mini coach") and the catalog is edited in
    Settings, so the two would drift the first time someone renamed a vehicle.

    A group above the per-run limit splits, and it is the *run* that needs seating — 105
    guests at a 40-cap venue is three 40-seat coaches, not one impossible 105-seater.
    Returns None when nothing in the catalog fits, so the picker opens unset rather than
    quietly recommending a vehicle that cannot do the job.
    """
    from .models import VehicleType

    limit = min(cap or MAX_COACH_SEATS, MAX_COACH_SEATS)
    runs = math.ceil(passengers / limit) if passengers > limit else 1
    per_run = math.ceil(passengers / runs)
    return (
        VehicleType.objects.filter(active=True, capacity__gte=per_run)
        .order_by("capacity", "sort_order")
        .first()
    )


def apply_vehicle_rate_card(reservation, vehicle: VehicleType | None) -> None:
    """Snapshot the vehicle's rate and rate-card minimum onto the reservation, in place.

    The reservation editor does this in the browser — app.js writes `draft.rate` and
    `draft.minHours` from the picked vehicle and `drafts.parse_draft` simply persists what
    was posted — so there has never been a server-side equivalent. Anything that assigns a
    vehicle outside that editor needs one, or the trip saves at rate 0 and the whole quote
    totals zero.

    *Which* minimum applies follows `reservation.trip_type`, the same rule the editor
    uses: an hourly trip billed at the transfer minimum quotes hours short. Set the trip
    type before calling this.
    """
    from apps.reservations.models import Reservation

    reservation.vehicle = vehicle
    reservation.rate = vehicle.rate if vehicle else 0
    if vehicle is None:
        reservation.min_hours = 0
    elif reservation.trip_type == Reservation.TripType.HOURLY:
        reservation.min_hours = vehicle.hourly_min_hours
    else:
        reservation.min_hours = vehicle.transfer_min_hours


def _apply_trip_window(reservation) -> None:
    """Give an hourly trip the end its billed hours imply; clear both for a transfer.

    Mirrors `reservations.drafts._derive_dropoff_and_hours`, which does the same for the
    reservation editor: without it an hourly leg reaches dispatch and the customer's
    itinerary with no end time. Switching a leg back to a transfer drops the hours it was
    billing as well as the derived end, or the transfer keeps quoting them.
    """
    from apps.reservations.models import Reservation

    if reservation.trip_type != Reservation.TripType.HOURLY:
        reservation.hours = 0
        reservation.dropoff_date = None
        reservation.dropoff_time = None
        return
    billed = reservation.billed_hours
    if not (reservation.pickup_date and reservation.pickup_time and billed):
        return
    end = datetime.combine(reservation.pickup_date, reservation.pickup_time) + timedelta(
        hours=float(billed)
    )
    reservation.dropoff_date, reservation.dropoff_time = end.date(), end.time()


@dataclass
class WeddingRebuild:
    """What a rebuild did, so the view can tell the agent."""

    updated: list
    created: list
    # Generated trips the plan no longer contains. Deliberately NOT deleted — see below.
    orphans: list


def rebuild_wedding_trips(lead: Lead, data: dict) -> WeddingRebuild:
    """Reconcile a lead's wedding trips against the posted plan.

    Matched on `Reservation.source_leg_id`, so an unchanged leg keeps its row — and with
    it its pricing, its LimoAnywhere reservation id and its dispatch assignment. The
    public `create_lead_from_wedding(lead=…)` path deletes and recreates, which is fine
    for a customer resending their own request pre-quote and catastrophic once the office
    has priced or booked it.

    A trip with a blank `source_leg_id` was added by hand in the reservation editor: it is
    never matched, never updated and never reported. The builder owns what it generated
    and nothing else.

    Removed legs come back as `orphans` rather than being deleted — the view asks.
    """
    from apps.public.services import (
        wedding_payload,
        wedding_service_type,
        wedding_sites,
        wedding_stop,
    )
    from apps.public.wedding import build_notes, is_time_sensitive
    from apps.reservations.models import Reservation, Stop

    service_type = wedding_service_type()
    sites = wedding_sites(data)
    vehicles = data.get("vehicles") or {}
    trip_types = data.get("trip_types") or {}
    billed_hours = data.get("hours") or {}
    existing = {r.source_leg_id: r for r in lead.reservations.exclude(source_leg_id="")}
    seen: set[str] = set()
    updated: list = []
    created: list = []

    for i, leg in enumerate(data["legs"]):
        leg_id = leg["id"]
        seen.add(leg_id)
        res = existing.get(leg_id)
        is_new = res is None
        if is_new:
            res = Reservation(
                lead=lead, source_leg_id=leg_id, trip_type=Reservation.TripType.TRANSFER
            )
        res.sort_order = i
        res.service_type = service_type
        res.pickup_date = data["wedding_date"]
        res.pickup_time = leg["time"]
        res.passengers = leg["pax"]
        # Each override applies only when the agent actually posted it: a rebuild after a
        # time change must not silently un-price a trip, nor flip an hourly shuttle back
        # to a transfer, because this pass happened not to mention it.
        if leg_id in trip_types:
            res.trip_type = trip_types[leg_id]
        if leg_id in billed_hours:
            res.hours = billed_hours[leg_id]
        # After the trip type, never before: the rate-card minimum depends on it.
        if leg_id in vehicles:
            apply_vehicle_rate_card(res, vehicles[leg_id])
        _apply_trip_window(res)
        res.save()
        # Stops are cheap and fully derived; rewriting both is simpler and safer than
        # diffing two rows, and nothing downstream holds on to a Stop pk.
        res.stops.all().delete()
        Stop.objects.bulk_create(
            [
                wedding_stop(res, 0, leg["from"], leg.get("from_sub", ""), sites),
                wedding_stop(res, 1, leg["to"], leg.get("to_sub", ""), sites),
            ]
        )
        res.refresh_pickup_timezone()
        (created if is_new else updated).append(res)

    orphans = [res for leg_id, res in existing.items() if leg_id not in seen]

    lead.notes = build_notes(
        wedding_date=data["wedding_date"],
        venue=data.get("venue"),
        ceremony=data.get("ceremony"),
        hotels=data.get("hotels") or [],
        hotels_tbd=bool(data.get("hotels_tbd")),
        groups=data["groups"],
        times_tbd=bool(data.get("times_tbd")),
        legs=data["legs"],
    )
    # The portal form has no contact fields, so the payload takes them off the lead —
    # the customer's resume link rehydrates from this same blob.
    lead.intake_payload = wedding_payload(
        {
            **data,
            "name": lead.contact.name,
            "email": lead.contact.email,
            "phone": lead.contact.phone,
        }
    )
    lead.has_alert = is_time_sensitive(data["wedding_date"], timezone.localdate())
    lead.save(update_fields=["notes", "intake_payload", "has_alert", "updated_at"])
    return WeddingRebuild(updated=updated, created=created, orphans=orphans)
