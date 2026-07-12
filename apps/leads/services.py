"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

if TYPE_CHECKING:
    from .models import Lead

from apps.integrations import podium
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.messaging import touchpoints
from apps.payments.models import PaymentPlan

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


def send_quote(lead: Lead, *, base_url: str) -> SendQuoteResult:
    """Create/refresh the deposit plan, transition the lead, stamp the send/expiry, and
    email the public quote-page link over Podium (best-effort). The transition + stamps
    commit even if the Podium send fails, so a missing write_messages scope degrades
    gracefully. The Stripe deposit Checkout itself happens later, on the quote page."""
    # 1. preconditions — nothing is written on failure
    if lead.status in (lead.Status.BOOKED, lead.Status.LOST):
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error=f"This quote is already {lead.get_status_display().lower()}.",
        )
    if lead.quote_total <= 0:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add at least one reservation before sending the quote.",
        )
    email = (lead.contact.email or "").strip()
    if not email:
        return SendQuoteResult(
            ok=False,
            http_status=400,
            error="Add a customer email before sending the quote.",
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

    # 5. stamp the send + expiry, reset the viewed flag, and (re)schedule touch-points
    lead.quote_sent_at = timezone.now()
    lead.quote_expires_at = compute_quote_expiry(lead)
    lead.quote_viewed_at = None
    lead.save(update_fields=["quote_sent_at", "quote_expires_at", "quote_viewed_at", "updated_at"])
    touchpoints.schedule_quote_sent(lead)

    # 6. deliver over Podium email — best-effort
    delivery: dict = {"sent": False, "recipient": email, "error": None}
    try:
        podium.send_message(
            identifier=email, channel_type="email", body=_quote_message(lead, plan, link)
        )
        delivery["sent"] = True
    except (PodiumAPIError, PodiumNotConnected) as exc:
        delivery["error"] = str(exc)

    return SendQuoteResult(
        ok=True, http_status=200, link=link, status=lead.status, delivery=delivery
    )
