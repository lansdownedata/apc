"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings
from django.core import signing
from django.template.defaultfilters import date as date_filter
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.explicit import record_update
from apps.core import outbound
from apps.messaging import events, rendering, sender
from apps.messaging.models import Delivery
from apps.payments.models import PaymentPlan

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .models import CustomerFeedback, Lead, VehicleType

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


class ReissueQuoteError(ValueError):
    """The lead has no quote to reissue (never quoted, or already booked/lost)."""


def reissue_quote(lead: Lead) -> datetime:
    """Bring an expired quote back to life (APC-25) — a distinct action from Send / Resend.

    The staff member has re-checked availability and pricing; this gives the quote a fresh
    expiry window (`compute_quote_expiry`), clears the viewed flag, and restarts the
    quote-nurture touch-points. It does **not** re-deliver the link — the customer's signed
    quote-page URL is unchanged and works again the moment the expiry moves; "Send quote"
    stays the way to email/text them. Returns the new expiry.
    """
    if lead.status != lead.Status.QUOTED:
        raise ReissueQuoteError("Only a sent, not-yet-booked quote can be reissued.")
    lead.quote_expires_at = compute_quote_expiry(lead)
    lead.quote_viewed_at = None
    lead.save(update_fields=["quote_expires_at", "quote_viewed_at", "updated_at"])
    events.emit("quote_sent", lead)
    return lead.quote_expires_at


def make_quote_page_url(lead: Lead, *, base_url: str) -> str:
    return f"{base_url}{reverse('quote_page', args=[make_deposit_token(lead)])}"


def make_pay_page_url(lead: Lead, *, base_url: str) -> str:
    return f"{base_url}{reverse('quote_pay', args=[make_deposit_token(lead)])}"


def make_terms_page_url(lead: Lead, *, base_url: str) -> str:
    """The terms-acceptance page for an order that never reaches the pay page (APC-93)."""
    return f"{base_url}{reverse('quote_terms', args=[make_deposit_token(lead)])}"


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


def _quote_values(lead: Lead, plan: PaymentPlan, link: str) -> dict[str, str]:
    """What the Send-quote message rows fill from this send (`fields._GROUPS["Quote"]`)."""
    return {
        "contact_name": lead.contact.name,
        "quote_total": f"${plan.quote_total:,.2f}",
        "deposit_pct": f"{plan.deposit_pct}",
        "deposit_amount": f"${plan.deposit_amount:,.2f}",
        "quote_valid_through": (
            date_filter(lead.quote_expires_at, "F j, Y") if lead.quote_expires_at else ""
        ),
        "quote_link": link,
    }


def _quote_email_context(lead: Lead, plan: PaymentPlan, link: str) -> dict:
    """Template context for the quote summary (templates/email/blocks/quote_summary.*)."""
    return {
        "contact_name": lead.contact.name,
        "quote_no": lead.quote_no,
        "quote_total": f"{plan.quote_total:,.2f}",
        "deposit_pct": plan.deposit_pct,
        "deposit_amount": f"{plan.deposit_amount:,.2f}",
        "on_account": lead.on_account,  # APC-105: no deposit line
    }


def send_quote(lead: Lead, *, base_url: str, channels: set[str] | None = None) -> SendQuoteResult:
    """Create/refresh the deposit plan, transition the lead, stamp the send/expiry, and
    deliver the public quote-page link on the selected channels.

    ``channels`` is any non-empty subset of {"email", "sms"}; ``None`` (the default) means
    both. The wording is the Send-quote message row's (``messaging.sender.send_now``): the
    branded email by our own mail, the short text by Podium. Delivery is per-channel
    best-effort — the NEW->QUOTED transition, the
    quote_sent_at/quote_expires_at stamps, the quote_viewed_at reset, the PaymentPlan
    snapshot, and touch-point scheduling all commit even if every channel fails to send, so a
    missing Podium scope or a broken mail relay degrades gracefully. The Stripe deposit
    Checkout itself happens later, on the quote page.
    """
    selected = channels or {"email", "sms"}

    # 1. preconditions — nothing is written on failure
    if not outbound.enabled():
        return SendQuoteResult(ok=False, http_status=409, error=outbound.OFF_MESSAGE)
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
    # Who the Send-quote row reaches on this order (APC-129); each chosen channel needs
    # someone with an address on it.
    key = "send_quote_on_account" if lead.on_account else "send_quote"
    people = sender.resolve_for(key, lead)
    if "email" in selected and not any(p.email and "email" in p.channels for p in people):
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add a customer email before sending the quote by email.",
        )
    if "sms" in selected and not any(p.phone and "sms" in p.channels for p in people):
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
        events.emit("quote_sent", lead)

    # 6. deliver on each selected channel through the Send-quote message row (an order
    # billed to an account has its own wording) to the people it lists — best-effort,
    # never rolls back the transition
    try:
        summary = _quote_email_context(lead, plan, link)
        sent = sender.send_now(
            key,
            lead,
            extra_ctx=_quote_values(lead, plan, link),
            blocks={
                "quote_summary": rendering.block("quote_summary", summary),
                "button": rendering.button("View your quote & reserve", link),
            },
            channels=selected,
            logo=True,
            preheader=rendering.preheader("quote_sent", summary),
        )
    except Exception as exc:  # noqa: BLE001 — delivery must never break the send
        logger.exception("quote %s: building its message failed", lead.pk)
        sent = sender.SendResult(sent=False, deliveries=[], errors=[str(exc)])
    delivery: dict = {}
    for channel in ("email", "sms"):
        if channel not in selected:
            continue
        mine = [d for d in sent.deliveries if d.channel == channel]
        ok = any(d.status == Delivery.Status.SENT for d in mine)
        error = None
        if not ok:
            error = next((d.reason for d in mine if d.reason), "") or "; ".join(sent.errors)
        address = next((d.address for d in mine if d.status == Delivery.Status.SENT), "") or next(
            (p.phone if channel == "sms" else p.email for p in people), ""
        )
        delivery[channel] = {"sent": ok, "recipient": address, "error": error or None}

    return SendQuoteResult(
        ok=True, http_status=200, link=link, status=lead.status, delivery=delivery
    )


@lru_cache(maxsize=1)
def terms_version() -> str:
    """A stable id for the T&C text a customer accepts (APC-55): the first 8 hex chars of
    the SHA-256 of the rendered `public/_terms.html`. Editing the terms is a new version
    automatically — nobody has to remember to bump a constant. Cached per process; a
    deploy is the only way the file changes."""
    text = render_to_string("public/_terms.html")
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def accept_terms(lead: Lead) -> bool:
    """Stamp the customer's acceptance the first time; never overwrite it. Returns whether
    this call stamped. Conditional UPDATE so two racing requests can't both stamp."""
    stamp = {"accepted_terms_at": timezone.now(), "accepted_terms_version": terms_version()}
    stamped = type(lead).objects.filter(pk=lead.pk, accepted_terms_at__isnull=True).update(**stamp)
    if stamped:
        record_update(lead, **stamp)
        lead.refresh_from_db(fields=["accepted_terms_at", "accepted_terms_version"])
    return bool(stamped)


def fee_terms_context(lead: Lead, config=None) -> dict:
    """What `public/_fee_terms.html` renders from: the cleaning fee of each vehicle type on
    the order's live trips (APC-95), the one hold they make (the highest of them), and the
    overtime rule table (APC-91) from TaskConfig, so the block never drifts from Settings.

    `fees` is `[(type name, fee), ...]`, highest first, one entry per type, types with no
    fee left out. `hold` is `Decimal("0")` when no trip is in a held vehicle; `hold_on` is
    whether a hold will be placed: the Tasks screen's "Place cleaning holds" switch, on an
    order that is held at all (`holds.eligible`: not billed to an account, not farmed in).
    """
    from apps.dispatch.selectors import CANCELLED_STATUSES
    from apps.payments import holds
    from apps.tasks.models import TaskConfig

    config = config or TaskConfig.load()
    rows = (
        lead.reservations.filter(vehicle__cleaning_fee__gt=0)
        .exclude(trip_status__in=CANCELLED_STATUSES)
        .order_by("-vehicle__cleaning_fee", "vehicle__name")
        .values_list("vehicle__name", "vehicle__cleaning_fee")
        .distinct()
    )
    fees = [(name, Decimal(fee)) for name, fee in rows]
    hold = fees[0][1] if fees else Decimal("0")
    # The hold is mentioned only while one will be placed: a customer is never told about a
    # hold the switch, or the way the order is paid, keeps from happening.
    hold_on = bool(config.hold_enabled) and holds.eligible(lead)
    return {"fees": fees, "hold": hold, "hold_on": hold_on, "config": config}


def fee_terms_html(lead: Lead, config=None) -> str:
    return render_to_string("public/_fee_terms.html", fee_terms_context(lead, config))


def fee_terms_version(lead: Lead, config=None) -> str:
    """Like `terms_version()` for the fee block (APC-94): the first 8 hex chars of the
    SHA-256 of the rendered text. Not cached: the per-type fees and the grace numbers are
    settings, and the order's vehicles change, so either is a new version."""
    return hashlib.sha256(fee_terms_html(lead, config).encode()).hexdigest()[:8]


def acknowledge_fees(lead: Lead, *, ip: str) -> bool:
    """Stamp the billing contact's acknowledgment of the cleaning fee and overtime rule the
    first time; never overwrite it. Returns whether this call stamped."""
    stamp = {
        "fees_acknowledged_at": timezone.now(),
        "fees_acknowledged_ip": ip or None,
        "fees_acknowledged_version": fee_terms_version(lead),
    }
    stamped = (
        type(lead).objects.filter(pk=lead.pk, fees_acknowledged_at__isnull=True).update(**stamp)
    )
    if stamped:
        record_update(lead, **stamp)
        lead.refresh_from_db(
            fields=["fees_acknowledged_at", "fees_acknowledged_ip", "fees_acknowledged_version"]
        )
    return bool(stamped)


class LeadStateError(Exception):
    """A lead cannot make the status change asked of it; the message says why."""


def mark_lost(lead: Lead, reason: str) -> Lead:
    """Mark a new or quoted lead Lost and withdraw every queued send of the order.

    A blank `reason` records "Marked lost". Booked and already-lost leads refuse with
    `LeadStateError`: a booked order is cancelled from the Orders console instead.
    """
    if not lead.can_transition(lead.Status.LOST):
        raise LeadStateError(
            "Booked orders are cancelled from the Orders console."
            if lead.status == lead.Status.BOOKED
            else "Already marked lost."
        )
    lead.status = lead.Status.LOST
    lead.lost_reason = (reason or "").strip() or "Marked lost"
    lead.save(update_fields=["status", "lost_reason", "updated_at"])
    events.cancel(lead, triggers=events.EVERY)
    return lead


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
    from apps.tasks import services as tasks

    if lead.status == lead.Status.LOST:
        raise BookLeadError("Lost leads cannot be booked.")

    already_booked = lead.status == lead.Status.BOOKED
    if not already_booked:
        lead.status = lead.Status.BOOKED
        lead.save(update_fields=["status", "updated_at"])
        events.cancel(lead)
    # Freeze each trip's total as booked (spec 2026-10-25 §4.4). Blanks only, so the
    # balance payment re-entering here never re-freezes an edited trip.
    from apps.reservations import pricing

    pricing.freeze_booked_totals(lead)

    plan, created = PaymentPlan.objects.get_or_create(lead=lead)
    if created or plan.quote_total == 0:
        plan.snapshot_total()
    if not already_booked:
        # After the totals are frozen and the plan exists: a confirmation already due goes
        # out on commit, which outside a transaction is right here.
        events.schedule_dates(lead)

    tasks.sync(lead)
    if already_booked:
        return lead

    try:
        la_sync.push_lead_bookings(lead)
    except Exception:
        logger.exception("LimoAnywhere push failed for lead %s", lead.pk)
    return lead


def group_fleet() -> list[VehicleType]:
    """The active vehicles offered for group transport, smallest first.

    The single source of what counts as a shuttle: a point of interest's "largest vehicle
    that fits" picker and its default ceiling both read this list. Nothing here is
    hardcoded — rename a vehicle, change its capacity or untick "group transport" in
    Settings and both follow on the next page load, with no deploy.
    """
    from .models import VehicleType

    return list(
        VehicleType.objects.filter(active=True, group_transport=True).order_by(
            "capacity", "sort_order"
        )
    )


def largest_group_capacity(fleet: list[VehicleType] | None = None) -> int | None:
    """Seats on our biggest shuttle — the ceiling a run splits across when a POI sets no
    smaller limit of its own. None when the catalog holds no group vehicle at all."""
    fleet = group_fleet() if fleet is None else fleet
    return fleet[-1].capacity if fleet else None


# --- customer feedback (APC-63) ---------------------------------------------------------


class FeedbackError(Exception):
    """The feedback form can't accept what was sent; the message is shown to the customer."""


def order_finished(lead: Lead) -> bool:
    """Every live trip on the order has run — APC-58's rule (Done, or past its end plus
    the post-trip grace; cancelled trips count as finished)."""
    from apps.tasks import post_trip
    from apps.tasks.models import TaskConfig

    grace = timedelta(hours=TaskConfig.load().post_trip_grace_hours)
    trips = list(lead.reservations.all())
    return post_trip.order_finished_at(trips, now=timezone.now(), grace=grace) is not None


def record_feedback(lead: Lead, *, rating, comment: str = "") -> CustomerFeedback:
    """Store (or edit) the order's feedback, then let the task engine react — a low rating
    opens a Customer Service follow-up."""
    from apps.tasks import services as tasks

    from .models import CustomerFeedback

    try:
        value = int(rating)
    except (TypeError, ValueError):
        value = 0
    if not 1 <= value <= 5:
        raise FeedbackError("Pick a rating from 1 to 5.")
    feedback, _ = CustomerFeedback.objects.update_or_create(
        lead=lead,
        defaults={
            "rating": value,
            "comment": (comment or "").strip()[:2000],
            "submitted_at": timezone.now(),
        },
    )
    tasks.sync(lead)
    return feedback


def feedback_context(lead: Lead) -> dict:
    """What `leads/_customer_feedback.html` reads: the feedback, and when it was sent in
    the order's first-trip zone with its abbreviation (never the viewer's). One query."""
    from zoneinfo import ZoneInfo

    from apps.tasks.models import format_local

    from .models import CustomerFeedback

    feedback = CustomerFeedback.objects.filter(lead=lead).first()
    if feedback is None:
        return {"feedback": None, "feedback_sent_display": ""}
    first = lead.reservations.order_by("pickup_date", "pickup_time", "pk").first()
    zone = ZoneInfo((first and first.pickup_timezone) or settings.TIME_ZONE)
    return {
        "feedback": feedback,
        "feedback_sent_display": format_local(feedback.submitted_at.astimezone(zone)),
    }
