"""APC-101 — each finished trip is charged or refunded on its own (spec 2026-10-25 §4.4).

Stripe is mocked throughout and the key is blanked so an unmocked call can't reach it.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_real_stripe(settings):
    settings.STRIPE_SECRET_KEY = ""
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _booked_order(*trips):
    """A booked order whose trips ran, each `(rate, hours, gratuity_pct)` frozen at booking."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    made = []
    for i, (rate, hours, pct) in enumerate(trips):
        trip = ReservationFactory(
            lead=lead,
            trip_type="hourly",
            pickup_date=timezone.localdate() - timedelta(days=2) + timedelta(days=i),
            pickup_time=time(18, 0),
            pickup_timezone="America/New_York",
            rate=Decimal(rate),
            hours=Decimal(hours),
            min_hours=0,
            gratuity_pct=Decimal(pct),
        )
        trip.booked_total = trip.line_total
        trip.save(update_fields=["booked_total"])
        made.append(trip)
    lead.refresh_from_db()
    plan = PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    return lead, plan, made


# --- Task 1: a charge can belong to a trip ---------------------------------------------


def test_a_charge_can_belong_to_a_trip():
    trip = ReservationFactory()
    charge = ChargeFactory(
        plan=PaymentPlanFactory(lead=trip.lead),
        kind=Charge.Kind.TRIP,
        reservation=trip,
        note="Wait time",
    )

    assert trip.charges.get() == charge
    assert charge.get_kind_display() == "Trip charge"
    assert charge.note == "Wait time"


# --- Task 2: what the order prepaid covers each trip ------------------------------------
from apps.payments import ledger, trip_money  # noqa: E402
from apps.payments.models import JournalEntry  # noqa: E402


def _collect(lead, amount, key, kind=JournalEntry.Kind.DEPOSIT_CAPTURED, charge_kind=None):
    """Seed collected cash the way the real flows do: a succeeded order-level charge plus its
    ledger entry."""
    plan = lead.payment
    charge = ChargeFactory(
        plan=plan,
        kind=charge_kind or Charge.Kind.DEPOSIT,
        amount=Decimal(amount),
        status=Charge.Status.SUCCEEDED,
        stripe_payment_intent_id=f"pi_{key}",
    )
    ledger.post_capture(lead=lead, amount=Decimal(amount), kind=kind, idempotency_key=key)
    return charge


def _two_trips():
    # A: 6 h × 185 + 20% = 1,332 on day 1; B: 1 h × 185 = 185 on day 2
    return _booked_order(("185", "6", "20"), ("185", "1", "0"))


def test_a_paid_in_full_order_covers_every_trip_at_its_booked_total():
    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "1517.00", "full")

    ma, mb = trip_money.money_for(a), trip_money.money_for(b)

    assert (ma.prepaid, ma.difference) == (Decimal("1332.00"), Decimal("0.00"))
    assert (mb.prepaid, mb.difference) == (Decimal("185.00"), Decimal("0.00"))


def test_a_partially_paid_order_allocates_the_deposit_in_pickup_order():
    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "758.50", "dep")

    ma, mb = trip_money.money_for(a), trip_money.money_for(b)

    assert (ma.prepaid, ma.difference) == (Decimal("758.50"), Decimal("573.50"))
    assert (mb.prepaid, mb.difference) == (Decimal("0.00"), Decimal("185.00"))


def test_a_trip_added_after_booking_has_no_prepaid_share():
    lead, _plan, (a, _b) = _two_trips()
    _collect(lead, "1517.00", "full")
    c = ReservationFactory(lead=lead, rate=Decimal("300"), hours=1, min_hours=0)
    c.booked_total = Decimal("0.00")
    c.save(update_fields=["booked_total"])

    mc = trip_money.money_for(c)

    assert (mc.prepaid, mc.difference) == (Decimal("0.00"), Decimal("300.00"))
    assert trip_money.money_for(a).prepaid == Decimal("1332.00")


def test_an_edit_changes_the_difference_not_the_prepaid_share():
    lead, _plan, (a, _b) = _two_trips()
    _collect(lead, "1517.00", "full")
    a.overtime_hours = Decimal("1")
    a.save(update_fields=["overtime_hours"])

    ma = trip_money.money_for(a)

    assert ma.prepaid == Decimal("1332.00")
    assert ma.difference == Decimal("222.00")  # 185 + 20% gratuity


def test_the_allocation_is_the_same_whichever_trip_is_processed_first():
    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "758.50", "dep")
    before = trip_money.money_for(a)
    ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.TRIP,
        reservation=b,
        amount=Decimal("185.00"),
        status=Charge.Status.SUCCEEDED,
    )

    after = trip_money.money_for(a)

    assert (after.prepaid, after.difference) == (before.prepaid, before.difference)
    assert trip_money.money_for(b).difference == Decimal("0.00")


def test_a_trip_refund_lowers_only_that_trips_allocation():
    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "1517.00", "full")
    ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.REFUND,
        reservation=a,
        amount=Decimal("100.00"),
        status=Charge.Status.SUCCEEDED,
    )

    assert trip_money.money_for(a).allocated == Decimal("1232.00")
    assert trip_money.money_for(b).allocated == Decimal("185.00")


def test_an_order_level_refund_lowers_the_prepaid_pool_from_the_last_trip_back():
    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "1517.00", "full")
    ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.REFUND,
        reservation=None,
        amount=Decimal("200.00"),
        status=Charge.Status.SUCCEEDED,
    )

    assert trip_money.money_for(a).prepaid == Decimal("1317.00")
    assert trip_money.money_for(b).prepaid == Decimal("0.00")


def test_a_cancelled_trip_takes_no_prepaid_money():
    from apps.reservations.models import Reservation

    lead, _plan, (a, b) = _two_trips()
    _collect(lead, "1517.00", "full")
    a.trip_status = Reservation.TripStatus.CANCELLED
    a.save(update_fields=["trip_status"])

    assert trip_money.money_for(a).prepaid == Decimal("0.00")
    assert trip_money.money_for(b).prepaid == Decimal("185.00")
