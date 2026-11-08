"""APC-95 — the cleaning hold on the order's money card (spec 2026-10-27 §5): one line under
the payment plan, with the time in the first held trip's zone and its abbreviation, then
what became of it. A decline and a hold that could not be placed read differently.
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead
from apps.payments import holds, reports
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

# 11:02 UTC on Nov 8 2026 is 3:02 AM in Los Angeles (PST) and 6:02 AM in New York.
PLACED = datetime(2026, 11, 8, 11, 2, tzinfo=UTC)
_types = count()


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.STRIPE_SECRET_KEY = ""
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _order(*, card=True, zone="America/Los_Angeles"):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    van = VehicleTypeFactory(name=f"Van {next(_types)}", cleaning_fee=Decimal("500"))
    trip = ReservationFactory(
        lead=lead,
        vehicle=van,
        pickup_date=PLACED.date(),
        pickup_time=time(9, 0),
        pickup_timezone=zone,
    )
    plan = PaymentPlanFactory(lead=lead, quote_total=lead.quote_total)
    if card:
        plan.stripe_customer_id, plan.stripe_payment_method_id = "cus_1", "pm_1"
        plan.card_brand, plan.card_last4 = "visa", "4242"
        plan.save()
    return lead, plan, trip


def _hold(plan, status=Charge.Status.AUTHORIZED, **kw):
    fields = {
        "amount": Decimal("500.00"),
        "note": "Cleaning hold",
        "authorized_at": PLACED,
        "capture_expires_at": PLACED + timedelta(days=7),
        "card_brand": "visa",
        "card_last4": "4242",
        **kw,
    }
    return plan.charges.create(
        kind=Charge.Kind.HOLD,
        status=status,
        stripe_payment_intent_id="pi_hold",
        idempotency_key=f"hold-{plan.pk}",
        **fields,
    )


def test_money_card_shows_the_hold_line_in_the_trips_zone(client):
    client.force_login(UserFactory(can_manage_payments=True))
    lead, plan, trip = _order()
    _hold(plan)

    card = reports.cleaning_hold(lead)
    body = client.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert card["hold_state"] == "placed"
    assert card["hold_tz_label"] == "PST"
    assert "Cleaning hold $500.00 · Visa ••4242 · placed Nov 8, 3:02 AM PST" in body


def test_the_card_falls_back_to_the_plans_when_the_row_has_none():
    lead, plan, trip = _order()
    _hold(plan, card_brand="", card_last4="")
    plan.card_brand, plan.card_last4 = "mastercard", "5555"
    plan.save()

    assert reports.cleaning_hold(lead)["hold_card"] == "Mastercard ••5555"


def test_a_released_hold_says_why():
    lead, plan, trip = _order()
    _hold(plan, Charge.Status.RELEASED, note="all held trips processed")

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "released"
    assert card["hold_detail"] == "released: all held trips processed"


def test_a_captured_hold_names_its_trip():
    lead, plan, trip = _order()
    _hold(
        plan,
        Charge.Status.SUCCEEDED,
        amount=Decimal("250.00"),
        captured_at=PLACED + timedelta(days=1),
        reservation=trip,
        note="Cleaning fee from the hold",
    )

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "captured"
    assert card["hold_detail"] == f"captured $250.00 for trip {trip.reference}"


def test_a_capture_not_yet_booked_says_so():
    lead, plan, trip = _order()
    _hold(plan, amount=Decimal("300.00"), captured_at=PLACED + timedelta(days=1))

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "captured"
    assert card["hold_detail"] == "captured $300.00, not yet booked to a trip"


def test_an_expired_hold():
    lead, plan, trip = _order()
    _hold(plan, Charge.Status.EXPIRED)

    card = reports.cleaning_hold(lead)

    assert (card["hold_state"], card["hold_detail"]) == ("expired", "expired at the bank")


def test_a_pending_hold():
    lead, plan, trip = _order()
    _hold(plan, Charge.Status.PENDING, authorized_at=None)

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "pending"
    assert card["hold_detail"] == "waiting on Stripe"
    assert card["hold_placed_at"] == ""


def test_a_card_decline_reads_declined(client):
    client.force_login(UserFactory(can_manage_payments=True))
    lead, plan, trip = _order()
    err = stripe.error.CardError("insufficient funds", "card", "card_declined")
    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        holds.place_hold(lead, now=PLACED)

    card = reports.cleaning_hold(lead)
    body = client.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert (card["hold_state"], card["hold_detail"]) == ("declined", "declined: insufficient funds")
    assert "declined: insufficient funds" in body


def test_a_hold_stripe_would_not_take_reads_could_not_be_placed():
    lead, plan, trip = _order()
    with patch.object(
        stripe.PaymentIntent, "create", return_value=MagicMock(id="pi_x", status="canceled")
    ):
        holds.place_hold(lead, now=PLACED)

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "not placed"
    assert card["hold_detail"] == "could not be placed: canceled"
    assert "declined" not in card["hold_detail"]


def test_no_card_on_file_reads_could_not_be_placed():
    lead, plan, trip = _order(card=False)
    holds.place_hold(lead, now=PLACED)

    card = reports.cleaning_hold(lead)

    assert card["hold_state"] == "not placed"
    assert card["hold_detail"] == "could not be placed: no card on file"
    assert card["hold_amount"] == Decimal("500.00")
    assert card["hold_card"] == ""


def test_no_hold_leaves_every_key_blank(client):
    client.force_login(UserFactory(can_manage_payments=True))
    lead, plan, trip = _order()

    card = reports.cleaning_hold(lead)
    body = client.get(reverse("order_detail", args=[lead.pk])).content.decode()

    assert card["hold_state"] == "" and card["hold_amount"] is None
    assert "Cleaning hold $" not in body


def test_the_quote_workspace_shows_the_line_too(client):
    client.force_login(UserFactory(can_manage_payments=True))
    lead, plan, trip = _order()
    _hold(plan, Charge.Status.RELEASED, note="order cancelled")

    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert "Cleaning hold $500.00 · Visa ••4242 · placed Nov 8, 3:02 AM PST — released" in body


def test_a_held_order_shows_the_fee_acknowledgment_state(client):
    client.force_login(UserFactory(can_manage_payments=True))
    lead, plan, trip = _order()

    body = client.get(reverse("order_detail", args=[lead.pk])).content.decode()

    # Before any hold row exists: the acknowledgment the confirmation now asks for.
    assert "Cleaning fee and overtime not yet acknowledged" in body
