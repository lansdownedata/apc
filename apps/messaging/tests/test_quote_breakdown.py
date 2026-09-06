"""`{quote_breakdown}` follows the same display rule as the quote page (spec 2026-09-05 §4).

The message and the page describe the same money. If one itemises and the other doesn't, the
customer is reconciling two documents.
"""

from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory
from apps.messaging.touchpoint_templates import build_context
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _breakdown(**res_kwargs):
    lead = LeadFactory()
    ReservationFactory(lead=lead, rate=Decimal("1538.50"), hours=Decimal("1"), **res_kwargs)
    return build_context(lead)["quote_breakdown"]


def test_a_flat_price_is_one_number():
    text = _breakdown()

    assert "$1,538.50" in text
    assert "Gratuity" not in text
    assert "Base fare" not in text


def test_gratuity_is_broken_out():
    text = _breakdown(gratuity_pct=Decimal("20"))

    assert "Base fare" in text
    assert "Gratuity (20%)" in text
    assert "$1,846.20" in text


def test_a_discount_is_broken_out_and_signed():
    text = _breakdown(discount_flat=Decimal("50"))

    assert "Discount" in text
    assert "-$50.00" in text


def test_the_message_never_carries_our_cost():
    text = _breakdown(affiliate_cost=Decimal("1000"), cost_ratio_pct=Decimal("65"))

    assert "1,000.00" not in text
    assert "65%" not in text
