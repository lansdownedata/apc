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
# Paid by the factor instead of a flat rate, the trip below owes its vendor 63.21% of
# 1952.80 — a number no fixture holds, only `Reservation.vendor_pay` can produce.
FACTOR_PAY = Decimal("1234.36")
# What must not appear, in every rendering these surfaces plausibly use.
FORBIDDEN = ("1234.56", "1,234.56", "63.21", "1234.6", "63.2", "1234.36", "1,234.36", "1234.4")


def _assert_clean(text: str, where: str) -> None:
    for needle in FORBIDDEN:
        assert needle not in text, f"{needle!r} leaked into {where}"


@pytest.fixture(params=["flat", "factor"])
def costed_lead(request):
    """Every surface is checked twice: a trip paying a flat rate, and one paying by factor."""
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(
        lead=lead,
        rate=Decimal("1952.80"),
        hours=Decimal("1"),
        min_hours=0,
        pickup_date=date(2026, 11, 14),
        pickup_time=time(9, 30),
        pickup_timezone="America/New_York",
        affiliate_cost=COST if request.param == "flat" else 0,
        cost_ratio_pct=RATIO,
    )
    return lead


def test_the_factor_fixture_really_owes_the_forbidden_number():
    trip = ReservationFactory(rate=Decimal("1952.80"), hours=1, min_hours=0, cost_ratio_pct=RATIO)
    assert trip.vendor_pay == FACTOR_PAY


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


def test_the_driver_page_never_shows_it(client, costed_lead, settings):
    """The driver runs the trip from this page; neither our cost nor their payout is on it."""
    from apps.dispatch.models import Assignment
    from apps.reservations.acknowledgements import make_driver_link_token
    from apps.reservations.models import Reservation
    from apps.reservations.services import set_trip_status

    settings.LOCATIONIQ_PUBLIC_KEY = "pk.public"
    costed_lead.status = Lead.Status.BOOKED
    costed_lead.save(update_fields=["status"])
    reservation = costed_lead.reservations.first()
    assignment = AssignmentFactory(
        reservation=reservation,
        vendor=VendorFactory(),
        status=Assignment.Status.CONFIRMED,
        payout=Decimal("987.65"),
    )
    set_trip_status(reservation, Reservation.TripStatus.DISPATCHED)

    resp = client.get(reverse("driver_link", args=[make_driver_link_token(assignment)]))

    assert resp.status_code == 200
    body = resp.content.decode()
    assert reservation.reference in body, "the live page rendered, not the dead view"
    _assert_clean(body, "the driver page")
    for payout in ("987.65", "987.6"):
        assert payout not in body, f"the payout {payout!r} leaked into the driver page"


# --- integrations ------------------------------------------------------------
def test_the_limoanywhere_booking_payload_never_carries_it(costed_lead):
    from apps.integrations.la_sync import build_booking_payload

    reservation = costed_lead.reservations.first()

    payload = build_booking_payload(reservation, search_result_id=1)

    _assert_clean(str(payload), "the LA booking payload")


def test_the_limoanywhere_rate_lookup_never_carries_it(costed_lead, monkeypatch):
    """The rate lookup goes out *before* the booking — it must be clean too."""
    from apps.integrations.la_sync import build_rate_lookup_payload

    # This payload geocodes its stops; stub the coordinates rather than spending a
    # billed LocationIQ lookup on a test about what the payload must not contain.
    monkeypatch.setattr(
        "apps.integrations.la_sync.geocoding.geocode_stop", lambda stop: (38.9, -77.4)
    )
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


# --- the driver link's messages ----------------------------------------------------------
def _booked_trip(lead):
    lead.status = Lead.Status.BOOKED
    lead.save(update_fields=["status"])
    return lead.reservations.first()


def _assert_no_money(text: str, where: str) -> None:
    """Our cost, the customer's price and the vendor's payout: none of it."""
    _assert_clean(text, where)
    for needle in ("1952.8", "1,952.8", "987.65", "987.6", "$"):
        assert needle not in text, f"{needle!r} leaked into {where}"


def test_the_trip_confirmed_email_never_shows_it(
    costed_lead, mailoutbox, django_capture_on_commit_callbacks
):
    from apps.dispatch import services

    reservation = _booked_trip(costed_lead)
    with django_capture_on_commit_callbacks(execute=True):
        services.assign_direct(
            reservation, VendorFactory(email="ops@vendor.example"), payout=Decimal("987.65")
        )

    [message] = [m for m in mailoutbox if m.subject.startswith("Trip confirmed")]
    _assert_no_money(message.subject, "the trip-confirmed subject")
    _assert_no_money(message.body, "the trip-confirmed email")
    _assert_no_money(message.alternatives[0][0], "the trip-confirmed email's HTML")


def test_the_driver_assignment_text_never_shows_it(
    costed_lead, mailoutbox, django_capture_on_commit_callbacks
):
    from unittest.mock import patch

    from apps.dispatch import services
    from apps.fleet.factories import DriverFactory

    reservation = _booked_trip(costed_lead)
    driver = DriverFactory(phone="+15715550142", email="sam@apc.example")
    with patch("apps.integrations.podium.send_message", return_value={"uid": "m"}) as podium:
        with django_capture_on_commit_callbacks(execute=True):
            services.assign_in_house(reservation, driver)

    # The customer may hear about their driver too; only the driver's own messages count.
    [call] = [c for c in podium.call_args_list if c.kwargs["identifier"] == "+15715550142"]
    _assert_no_money(call.kwargs["body"], "the driver assignment text")
    [message] = [m for m in mailoutbox if m.to == ["sam@apc.example"]]
    _assert_no_money(message.body, "the driver assignment email")
    _assert_no_money(message.alternatives[0][0], "the driver assignment email's HTML")
