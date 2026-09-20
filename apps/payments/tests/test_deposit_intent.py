"""The deposit is collected on our own page now — a PaymentIntent, not a Checkout Session.

Hosted Checkout (`create_deposit_checkout` + `quote_book`) is gone (spec 2026-08-30 §8).
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services
from apps.payments.factories import PaymentPlanFactory

pytestmark = pytest.mark.django_db


def _plan(**kwargs):
    kwargs.setdefault("quote_total", Decimal("2670.00"))
    kwargs.setdefault("deposit_pct", 50)
    kwargs.setdefault("stripe_customer_id", "cus_1")
    lead = kwargs.pop("lead", None) or LeadFactory(status=Lead.Status.QUOTED)
    return PaymentPlanFactory(lead=lead, **kwargs)


def _intents():
    return [MagicMock(id=f"pi_{n}", client_secret=f"pi_{n}_secret_x") for n in range(1, 4)]


def test_the_hosted_checkout_helpers_are_all_gone():
    assert not hasattr(services, "create_deposit_checkout")
