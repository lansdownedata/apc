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
