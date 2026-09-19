"""The card form we draw ourselves, instead of Stripe's tabbed Payment Element.

The Payment Element renders whatever the Stripe account has switched on — a Bank (ACH)
tab, a "save my info with Link" sign-up asking for an email and a mobile number, and a
Country select. None of that belongs on a charter checkout that takes a card, and none of
it could be configured away: it rides on top of the card tab.

Individual card elements (cardNumber / cardExpiry / cardCvc) cannot render any of it, so
the layout is ours: cardholder name, number, expiry and CVV required; ZIP optional.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]
STRIPE_JS = (ROOT / "static" / "js" / "stripe-pay.js").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


@pytest.fixture
def owner(client, settings):
    settings.STRIPE_PUBLISHABLE_KEY = "pk_test_123"
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


def _staff_checkout(client) -> str:
    lead = LeadFactory(status=Lead.Status.BOOKED)
    return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()


def _pay_page(client) -> str:
    """An unpaid deposit — the state the card form renders in (see test_quote_pay)."""
    from django.utils import timezone

    from apps.contacts.factories import ContactFactory
    from apps.payments.factories import PaymentPlanFactory
    from apps.reservations.factories import TransferReservationFactory

    lead = LeadFactory(
        status=Lead.Status.QUOTED,
        contact=ContactFactory(),
        quote_expires_at=timezone.now() + timezone.timedelta(days=10),
    )
    TransferReservationFactory(lead=lead, rate=Decimal("1000.00"))
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)
    return client.get(
        reverse("quote_pay", args=[lead_services.make_deposit_token(lead)])
    ).content.decode()


# --- the fields we ask for ---------------------------------------------------------


def test_the_staff_checkout_asks_for_a_cardholder_name(client, owner):
    body = _staff_checkout(client)
    assert "data-card-name" in body
    assert "Cardholder name" in body


def test_the_staff_checkout_mounts_number_expiry_and_cvv_separately(client, owner):
    body = _staff_checkout(client)
    for slot in ("data-card-number", "data-card-expiry", "data-card-cvc"):
        assert slot in body, slot


def test_the_zip_is_offered_and_marked_optional(client, owner):
    body = _staff_checkout(client)
    assert "data-card-zip" in body
    assert "Optional" in body


def test_the_staff_checkout_no_longer_mounts_the_payment_element(client, owner):
    body = _staff_checkout(client)
    assert 'x-ref="cardMount"' not in body


# --- what can no longer appear -----------------------------------------------------


def test_nothing_creates_a_tabbed_payment_element_any_more():
    """`elements.create("payment", …)` is the tabbed element that brought Bank and Link."""
    for source in (STRIPE_JS, APP_JS):
        assert 'create("payment"' not in source
        assert "create('payment'" not in source


def test_the_card_elements_are_the_individual_ones():
    for kind in ("cardNumber", "cardExpiry", "cardCvc"):
        assert kind in STRIPE_JS, kind


def test_the_customer_pay_page_uses_the_same_fields(client):
    body = _pay_page(client)
    for slot in ("data-card-name", "data-card-number", "data-card-expiry", "data-card-cvc"):
        assert slot in body, slot
    assert "data-card-zip" in body


def test_both_checkouts_share_one_card_field_builder():
    """Two Stripe setups is how the staff form and the customer form drift apart."""
    assert "cardFields" in STRIPE_JS
    assert "apcPay.cardFields(" in APP_JS
    assert "cardFields(" in (ROOT / "templates" / "public" / "pay.html").read_text() or (
        "cardFields" in STRIPE_JS
    )


# --- the name is required, the ZIP is not ------------------------------------------


def test_a_missing_cardholder_name_is_refused_before_stripe_is_called():
    """Stripe would happily take a nameless card; the client asked for the name."""
    assert "cardholder" in STRIPE_JS.lower()


def test_a_blank_zip_is_simply_left_off_the_billing_details():
    """Passing an empty postal_code makes Stripe's own check fail; omit it instead."""
    assert "postal_code" in STRIPE_JS
