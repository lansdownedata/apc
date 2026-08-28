"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from apps.integrations import podium
from apps.messaging import touchpoints
from apps.notifications.email import send_html_email
from apps.payments.models import PaymentPlan

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .models import Lead

_DEPOSIT_SALT = "quote-deposit"


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
    pickup = timezone.make_aware(
        datetime.combine(first.pickup_date, first.pickup_time or time(0, 0))
    )
    cutoff = pickup - timedelta(days=days)
    return cutoff if cutoff > timezone.now() else pickup


def make_quote_page_url(lead: Lead, *, base_url: str) -> str:
    return f"{base_url}{reverse('quote_page', args=[make_deposit_token(lead)])}"


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
    """Convert a quote to Booked without recording a payment.

    Same side-effects as the Stripe deposit webhook minus the charge: status,
    pending touch-points, a PaymentPlan snapshot if missing, and a best-effort
    LimoAnywhere / Zapier push. Idempotent when already Booked. Lost leads refuse.
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
