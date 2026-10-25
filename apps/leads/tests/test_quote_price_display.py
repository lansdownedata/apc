"""The public quote page's price display (spec 2026-09-05 §4).

Client rule: no gratuity and no other line item -> one total. Extras -> shown separately,
above the total.

The label this replaces read "Vehicle Subtotal" over `line_total`, which already includes
gratuity — a total called a subtotal when there were no extras, and an invisible gratuity
when there were.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _page(client, **res_kwargs):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead, rate=Decimal("1538.50"), hours=Decimal("1"), **res_kwargs)
    resp = client.get(reverse("quote_page", args=[services.make_deposit_token(lead)]))
    assert resp.status_code == 200
    return resp.content.decode()


def test_a_flat_price_shows_one_total(client):
    body = _page(client)

    assert "1,538.50" in body
    assert "Total" in body


def test_a_flat_price_never_says_subtotal(client):
    """ "Subtotal" with nothing to sub-total makes the customer wait for a second shoe."""
    body = _page(client)

    assert "Subtotal" not in body


def test_gratuity_is_shown_on_its_own_line(client):
    body = _page(client, gratuity_pct=Decimal("20"))

    assert "Base fare" in body
    assert "Gratuity (20%)" in body
    assert "307.70" in body  # the gratuity itself, visible rather than folded in
    assert "1,846.20" in body  # the total


def test_a_discount_is_shown_on_its_own_line(client):
    body = _page(client, discount_flat=Decimal("50"))

    assert "Discount" in body
    assert "50.00" in body
    assert "1,488.50" in body


def test_the_customer_never_sees_our_cost_or_ratio(client):
    """Belt-and-braces alongside the T7 leak tests: these are internal, full stop."""
    body = _page(client, affiliate_cost=Decimal("1000"), cost_ratio_pct=Decimal("65"))

    assert "1,000.00" not in body
    assert "65%" not in body


def test_overtime_and_extras_are_lines_and_the_page_total_follows(client):
    """A trip edited after the quote was sent: the lines sum to the total shown, which is
    the live trip total, not the plan's snapshot (spec 2026-10-25 §3)."""
    from apps.payments.factories import PaymentPlanFactory
    from apps.reservations.factories import TripExtraFactory

    lead = LeadFactory(status=Lead.Status.QUOTED)
    trip = ReservationFactory(
        lead=lead, trip_type="hourly", rate=Decimal("200"), hours=Decimal("3"), min_hours=0
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("600.00"), deposit_pct=50)
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])
    TripExtraFactory(reservation=trip, kind="tolls", label="Tolls", amount=Decimal("12.50"))

    body = client.get(
        reverse("quote_page", args=[services.make_deposit_token(lead)])
    ).content.decode()

    assert "Overtime (1 hr)" in body and "200.00" in body
    assert "Tolls" in body and "12.50" in body
    assert "812.50" in body  # 600 + 200 + 12.50, live
    assert "300.00" in body  # the deposit still reads the snapshot: 50% of 600
