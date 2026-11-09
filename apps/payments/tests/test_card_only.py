"""Every intent we create is locked to `card` server-side (spec 2026-08-30 §4).

The lock makes an accidental change to the Dashboard's enabled methods unable to surface a
new method on any surface, including the webhook-driven and off-session charges.

Stripe API 2026-09-30.endive (stripe-python 16's default) refuses `payment_method_types`
with `payment_method_types_no_longer_supported`; the lock is `allowed_payment_method_types`,
which 2026-07-29.dahlia introduced. These tests run the real stripe-python request encoding
against a fake HTTP layer that answers the way endive does, so a create that would be
refused in production is refused here too. Patching `PaymentIntent.create` (as these tests
once did) never encodes a request, which is how the removed parameter went unnoticed.
"""

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import stripe

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import cards, holds, services, trip_money
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.payments.tests.test_trip_charges import _owing_trip

pytestmark = pytest.mark.django_db

_CARD = {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}


class EndiveStripe(stripe.HTTPClient):
    """Answers like Stripe on API 2026-09-30.endive, and records what was sent."""

    name = "endive-fake"

    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, post_data=None):
        path = urlparse(url).path
        body = parse_qs(post_data or "")
        self.sent.append((method, path, body))
        if any(key.startswith("payment_method_types") for key in body):
            return self._error(
                "payment_method_types_no_longer_supported",
                "The `payment_method_types` parameter is no longer supported.",
            )
        if path.startswith("/v1/setup_intents"):
            return self._ok({"id": "seti_1", "object": "setup_intent", "client_secret": "s_1"})
        if path.startswith("/v1/payment_intents"):
            amount = int(body.get("amount", ["0"])[0])
            return self._ok(
                {
                    "id": "pi_1",
                    "object": "payment_intent",
                    "status": "succeeded",
                    "amount": amount,
                    "amount_received": amount,
                    "client_secret": "pi_1_secret_x",
                    "metadata": {},
                    "payment_method": {"id": "pm_1", "object": "payment_method", "card": _CARD},
                }
            )
        return self._error("resource_missing", f"No fake for {method} {path}", status=404)

    def close(self):
        pass

    def created(self, resource: str) -> dict:
        """The body of the one create request sent to `/v1/<resource>`."""
        bodies = [b for m, p, b in self.sent if m == "post" and p == f"/v1/{resource}"]
        assert len(bodies) == 1, f"expected one {resource} create, sent {self.sent}"
        return bodies[0]

    @staticmethod
    def _ok(obj):
        return json.dumps(obj), 200, {}

    @staticmethod
    def _error(code, message, status=400):
        error = {"type": "invalid_request_error", "code": code, "message": message}
        return json.dumps({"error": error}), status, {}


@pytest.fixture
def endive(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_test_fake"
    fake = EndiveStripe()
    monkeypatch.setattr(stripe, "default_http_client", fake)
    monkeypatch.setattr("apps.integrations.la_sync.push_lead_bookings", lambda *a, **k: None)
    return fake


def _locked_to_card(body: dict) -> bool:
    return body.get("allowed_payment_method_types[0]") == ["card"] and not any(
        key.startswith("payment_method_types") for key in body
    )


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("1000.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def test_customer_payment_intent_is_card_only(endive):
    _charge, secret = services.create_admin_payment_intent(_plan(), Decimal("400.00"))

    assert secret == "pi_1_secret_x"
    assert _locked_to_card(endive.created("payment_intents"))


def test_balance_charge_is_card_only(endive):
    charge = services.charge_balance(_plan(stripe_payment_method_id="pm_1"))

    assert charge.status == Charge.Status.SUCCEEDED
    assert _locked_to_card(endive.created("payment_intents"))


def test_charge_saved_card_is_card_only(endive):
    charge = services.charge_saved_card(_plan(stripe_payment_method_id="pm_1"), Decimal("400.00"))

    assert charge.status == Charge.Status.SUCCEEDED
    assert _locked_to_card(endive.created("payment_intents"))


def test_card_vault_setup_intent_is_card_only(endive):
    contact = ContactFactory(stripe_customer_id="cus_contact")

    assert cards.start_add_card(contact) == "s_1"
    assert _locked_to_card(endive.created("setup_intents"))


def test_cleaning_hold_intent_is_card_only(endive):
    plan = _plan(stripe_payment_method_id="pm_1")
    charge = ChargeFactory(plan=plan, amount=Decimal("250.00"), status=Charge.Status.PENDING)

    intent = holds._create_intent(plan, charge)

    assert intent.id == "pi_1"
    assert _locked_to_card(endive.created("payment_intents"))


def test_trip_charge_is_card_only(endive):
    _lead, _plan_, trip = _owing_trip()

    charge = trip_money.charge_trip(trip, Decimal("222.00"), note="Overtime")

    assert charge.status == Charge.Status.SUCCEEDED
    assert _locked_to_card(endive.created("payment_intents"))
