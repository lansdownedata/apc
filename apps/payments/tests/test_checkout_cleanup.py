"""Leftovers from the hosted-Checkout era, and one real gap it left behind.

Hosted Checkout went on 2026-08-30 and the card-only flow replaced it, but the sweep was
never finished: two dead endpoints, a customer-facing return URL filed under the staff
portal, a button labelled as something it does not do, and a metadata `kind` the webhook
does not understand.

That last one is not cosmetic. `charge_saved_card` records a BALANCE charge but stamps the
intent `kind="admin"`, which `webhooks._SUCCESS_KINDS` has no entry for — so a
`payment_intent.succeeded` for it returns early. The inline reconcile hides that until the
one time it matters: Stripe takes the money, the inline call fails, and the webhook safety
net skips the event. The money is gone and no Charge is ever marked paid.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.urls import NoReverseMatch, reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services, webhooks
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge

pytestmark = pytest.mark.django_db


def _plan(**over):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    return PaymentPlanFactory(
        lead=lead,
        quote_total=Decimal("1000.00"),
        deposit_pct=50,
        stripe_customer_id="cus_1",
        stripe_payment_method_id="pm_1",
        **over,
    )


# --- the gap: a saved-card charge the webhook cannot reconcile -----------------------


def test_a_saved_card_charge_is_stamped_with_a_kind_the_webhook_knows():
    plan = _plan()
    with patch.object(services, "_stripe") as s:
        s.return_value.PaymentIntent.create.return_value = MagicMock(id="pi_saved")
        with patch.object(services, "record_payment") as rec:
            rec.return_value = MagicMock()
            services.charge_saved_card(plan, "100.00")
    kind = s.return_value.PaymentIntent.create.call_args.kwargs["metadata"]["kind"]
    assert kind in webhooks._SUCCESS_KINDS, f"{kind!r} would be skipped by the webhook"


def test_the_kind_matches_the_charge_it_actually_records():
    plan = _plan()
    with patch.object(services, "_stripe") as s:
        s.return_value.PaymentIntent.create.return_value = MagicMock(id="pi_saved")
        with patch.object(services, "record_payment") as rec:
            rec.return_value = MagicMock()
            services.charge_saved_card(plan, "100.00")
    kind = s.return_value.PaymentIntent.create.call_args.kwargs["metadata"]["kind"]
    assert webhooks._SUCCESS_KINDS[kind] == Charge.Kind.BALANCE


# --- dead code from the hosted-Checkout era -----------------------------------------


@pytest.mark.parametrize("gone", ["create_setup_intent", "create_deposit_intent"])
def test_the_dead_service_functions_are_gone(gone):
    assert not hasattr(services, gone)


def test_the_dead_setup_intent_endpoint_is_gone():
    with pytest.raises(NoReverseMatch):
        reverse("order_setup_intent", args=[1])


def test_nothing_still_describes_hosted_checkout():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    source = (root / "apps" / "payments" / "services.py").read_text()
    assert "Checkout +" not in source
    assert "deposit checkout" not in source.lower()


# --- the customer's 3-D Secure return, filed where customers' URLs live --------------


def test_the_card_return_is_a_public_quote_url_not_a_portal_one():
    assert reverse("quote_deposit_success", args=["tok"]).startswith("/quote/")


def test_the_old_portal_path_still_works(client):
    """It is in customers' inboxes; it has to keep landing somewhere."""
    resp = client.get("/portal/leads/quote/deposit/success/sometoken/")
    assert resp.status_code in (301, 302)
    assert "/quote/" in resp["Location"]


# --- two buttons, one label ----------------------------------------------------------


def test_only_one_control_on_the_lead_says_send_payment_link(client, settings):
    """The other one sends the QUOTE page — labelling both the same is how an agent
    sends a customer the wrong link."""
    settings.STRIPE_PUBLISHABLE_KEY = "pk_test_123"
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    lead = _plan().lead
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert body.count("Send payment link") == 1


# --- nothing to take ------------------------------------------------------------------


def test_take_payment_is_not_offered_on_a_fully_paid_order(client, settings):
    settings.STRIPE_PUBLISHABLE_KEY = "pk_test_123"
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    plan = _plan()
    from apps.payments import ledger
    from apps.payments.models import JournalEntry

    ledger.post_capture(
        lead=plan.lead,
        amount=Decimal("1000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key=f"paid-{plan.lead_id}",
    )
    body = client.get(reverse("lead_detail", args=[plan.lead_id])).content.decode()
    assert "Take payment" not in body
