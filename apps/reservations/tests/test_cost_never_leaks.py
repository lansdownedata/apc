"""`affiliate_cost` and `cost_ratio_pct` are internal (spec 2026-09-05 §3.6).

The affiliate seeing what we sold their trip for is a business problem, not a display bug —
and so is the customer seeing what we paid. These assert on the NUMBERS rather than the field
names, so a value reaching a surface under any label still fails.

The cost is chosen to be unmistakable: 1234.56 appears nowhere else in a fixture.
"""

from datetime import date, time
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.dispatch.factories import AssignmentFactory, VendorFactory
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

COST = Decimal("1234.56")
RATIO = Decimal("63.21")
# What must not appear, in every rendering these surfaces plausibly use.
FORBIDDEN = ("1234.56", "1,234.56", "63.21", "1234.6", "63.2")


def _assert_clean(text: str, where: str) -> None:
    for needle in FORBIDDEN:
        assert needle not in text, f"{needle!r} leaked into {where}"


@pytest.fixture
def costed_lead():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(
        lead=lead,
        rate=Decimal("1952.80"),
        hours=Decimal("1"),
        min_hours=0,
        pickup_date=date(2026, 11, 14),
        pickup_time=time(9, 30),
        pickup_timezone="America/New_York",
        affiliate_cost=COST,
        cost_ratio_pct=RATIO,
    )
    return lead


# --- customer-facing ---------------------------------------------------------
def test_the_public_quote_page_never_shows_it(client, costed_lead):
    token = lead_services.make_deposit_token(costed_lead)

    body = client.get(reverse("quote_page", args=[token])).content.decode()

    _assert_clean(body, "the public quote page")


def test_the_pay_page_never_shows_it(client, costed_lead):
    token = lead_services.make_deposit_token(costed_lead)

    resp = client.get(reverse("quote_pay", args=[token]))

    _assert_clean(resp.content.decode(), "the pay page")


def test_the_quote_message_never_carries_it(costed_lead):
    from apps.messaging.touchpoint_templates import build_context

    context = build_context(costed_lead)

    _assert_clean(" ".join(str(v) for v in context.values()), "the touch-point context")


# --- vendor-facing -----------------------------------------------------------
def test_the_vendor_offer_email_never_shows_our_sell_price_or_cost(costed_lead):
    """The affiliate is quoted a payout. What we sell it for is none of their business."""
    from apps.dispatch.services import offer_email_context

    reservation = costed_lead.reservations.first()
    assignment = AssignmentFactory(
        reservation=reservation, vendor=VendorFactory(), payout=Decimal("900.00")
    )

    context = offer_email_context(assignment)

    _assert_clean(" ".join(str(v) for v in context.values()), "the vendor offer context")


def test_the_affiliate_trip_sheet_never_shows_it(costed_lead):
    from apps.reservations.services import trip_sheet_text

    reservation = costed_lead.reservations.first()
    AssignmentFactory(reservation=reservation, vendor=VendorFactory(), payout=Decimal("900.00"))

    _assert_clean(trip_sheet_text(reservation), "the trip sheet")


# --- integrations ------------------------------------------------------------
def test_the_limoanywhere_booking_payload_never_carries_it(costed_lead):
    from apps.integrations.la_sync import build_booking_payload

    reservation = costed_lead.reservations.first()

    payload = build_booking_payload(reservation, search_result_id=1)

    _assert_clean(str(payload), "the LA booking payload")


def test_the_limoanywhere_rate_lookup_never_carries_it(costed_lead):
    """The rate lookup goes out *before* the booking — it must be clean too."""
    from apps.integrations.la_sync import build_rate_lookup_payload

    reservation = costed_lead.reservations.first()

    _assert_clean(str(build_rate_lookup_payload(reservation)), "the LA rate-lookup payload")


def test_the_gnet_payload_never_carries_it(costed_lead):
    from apps.integrations.gnet import build_send_payload

    reservation = costed_lead.reservations.first()
    assignment = AssignmentFactory(
        reservation=reservation,
        vendor=VendorFactory(gnet_grid_id="GRID123"),
        payout=Decimal("900.00"),
    )

    _assert_clean(str(build_send_payload(assignment)), "the GNet farm-out payload")
