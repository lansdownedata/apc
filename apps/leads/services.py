"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import stripe
from django.core import signing

from apps.integrations import podium
from apps.integrations.podium import PodiumAPIError, PodiumNotConnected
from apps.payments.models import PaymentPlan
from apps.payments.services import create_deposit_checkout

_DEPOSIT_SALT = "quote-deposit"


def make_deposit_token(lead) -> str:
    """An opaque, signed token encoding the lead id for the public deposit pages."""
    return signing.dumps({"lead": lead.pk}, salt=_DEPOSIT_SALT)


def read_deposit_token(token: str):
    """Return the Lead for a signed token. Raises BadSignature if forged/tampered."""
    from .models import Lead

    data = signing.loads(token, salt=_DEPOSIT_SALT)
    return Lead.objects.get(pk=data["lead"])


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


def _quote_message(lead, plan, link: str) -> str:
    contact = lead.contact
    return (
        f"Hi {contact.name}, here's your All Pro Charter quote {lead.quote_no} "
        f"for ${plan.quote_total:,.2f}. To confirm, pay your {plan.deposit_pct}% "
        f"deposit of ${plan.deposit_amount:,.2f} here: {link}"
    )


def send_quote(lead, *, success_url: str, cancel_url: str) -> SendQuoteResult:
    """Create/refresh the deposit plan, build the Stripe link, transition the lead, and
    email the link over Podium (best-effort). The transition + link commit even if the
    Podium send fails, so a missing write_messages scope degrades gracefully."""
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

    # 3. Stripe deposit Checkout link (sets deposit_status=REQUESTED)
    try:
        link = create_deposit_checkout(plan, success_url=success_url, cancel_url=cancel_url)
    except stripe.error.StripeError as exc:
        msg = (
            getattr(exc, "user_message", None)
            or getattr(getattr(exc, "error", None), "message", None)
            or str(exc)
        )
        return SendQuoteResult(
            ok=False, http_status=502, error=f"Could not create the deposit link: {msg}"
        )

    # 4. transition NEW -> QUOTED (idempotent; a QUOTED re-send stays QUOTED)
    if lead.status == lead.Status.NEW:
        lead.status = lead.Status.QUOTED
        lead.save(update_fields=["status", "updated_at"])

    # 5. deliver over Podium email — best-effort
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
