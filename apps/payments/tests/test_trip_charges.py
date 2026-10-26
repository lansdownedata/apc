"""APC-101 — each finished trip is charged or refunded on its own (spec 2026-10-25 §4.4).

Stripe is mocked throughout and the key is blanked so an unmocked call can't reach it.
"""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import ledger, services, trip_money, webhooks
from apps.payments import tasks as payment_tasks
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge, JournalEntry, PaymentPlan
from apps.reservations.factories import ReservationFactory
from apps.tasks.models import Task

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


# --- Task 3: charge a trip's difference ---------------------------------------------------


def _card(plan):
    plan.stripe_customer_id = "cus_1"
    plan.stripe_payment_method_id = "pm_1"
    plan.card_brand, plan.card_last4 = "visa", "4242"
    plan.save()
    return plan


def _intent(pi_id="pi_trip", *, amount=22200, status="succeeded"):
    return MagicMock(
        id=pi_id,
        status=status,
        amount=amount,
        client_secret=f"{pi_id}_secret",
        payment_method=MagicMock(id="pm_1", card=MagicMock(brand="visa", last4="4242")),
    )


def _stripe(intent=None):
    intent = intent or _intent()
    return (
        patch.object(stripe.PaymentIntent, "create", return_value=intent),
        patch.object(stripe.PaymentIntent, "retrieve", return_value=intent),
    )


def _owing_trip():
    """A paid-in-full order whose first trip then grew by an hour: it owes $222."""
    lead, plan, (a, _b) = _two_trips()
    _collect(lead, "1517.00", "full")
    _card(plan)
    a.overtime_hours = Decimal("1")
    a.save(update_fields=["overtime_hours"])
    return lead, plan, a


def test_charge_trip_takes_the_difference_off_the_card_and_tags_the_trip():
    lead, _plan, a = _owing_trip()
    create_p, retrieve_p = _stripe()

    with create_p as create, retrieve_p:
        charge = trip_money.charge_trip(a, Decimal("222.00"), note="Overtime")

    kwargs = create.call_args.kwargs
    assert kwargs["amount"] == 22200
    assert kwargs["off_session"] is True and kwargs["confirm"] is True
    assert kwargs["metadata"]["kind"] == "trip"
    assert kwargs["metadata"]["reservation_id"] == str(a.pk)
    charge.refresh_from_db()
    assert (charge.kind, charge.status, charge.reservation) == ("trip", "succeeded", a)
    assert (charge.card_brand, charge.card_last4, charge.note) == ("visa", "4242", "Overtime")
    entry = JournalEntry.objects.get(charge=charge)
    assert entry.kind == JournalEntry.Kind.BALANCE_CAPTURED and entry.reservation == a
    assert entry.is_balanced
    assert trip_money.money_for(a).difference == Decimal("0.00")
    assert services.remaining_balance(lead) == Decimal("0.00")


@pytest.mark.parametrize("amount", ["0", "-5"])
def test_charge_trip_refuses_zero_and_negative(amount):
    _lead, _plan, a = _owing_trip()
    with pytest.raises(services.PaymentError):
        trip_money.charge_trip(a, Decimal(amount))


def test_charge_trip_refuses_without_a_card():
    lead, plan, (a, _b) = _two_trips()
    with pytest.raises(services.PaymentError, match="No card on file"):
        trip_money.charge_trip(a, Decimal("10.00"))
    assert not plan.charges.exists()


def test_a_decline_records_a_failed_trip_charge_and_alerts_without_failing_the_balance():
    lead, plan, a = _owing_trip()
    plan.balance_status = PaymentPlan.BalanceStatus.PAID
    plan.save(update_fields=["balance_status"])
    err = stripe.error.CardError("Your card was declined.", "card", "card_declined")

    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        with pytest.raises(services.PaymentError, match="declined"):
            trip_money.charge_trip(a, Decimal("222.00"))

    charge = plan.charges.get(kind=Charge.Kind.TRIP)
    assert charge.status == Charge.Status.FAILED and charge.reservation == a
    plan.refresh_from_db()
    lead.refresh_from_db()
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID
    assert lead.has_alert is True
    assert Notification.objects.filter(lead=lead, kind=Notification.Kind.BALANCE_FAILED).exists()


def test_a_retry_after_a_decline_makes_a_new_attempt_with_a_new_key():
    _lead, plan, a = _owing_trip()
    err = stripe.error.CardError("Declined.", "card", "card_declined")
    with patch.object(stripe.PaymentIntent, "create", side_effect=err):
        with pytest.raises(services.PaymentError):
            trip_money.charge_trip(a, Decimal("222.00"))
    create_p, retrieve_p = _stripe()

    with create_p as create, retrieve_p:
        trip_money.charge_trip(a, Decimal("222.00"))

    keys = sorted(
        plan.charges.filter(kind=Charge.Kind.TRIP).values_list("idempotency_key", flat=True)
    )
    assert len(keys) == 2 and create.call_args.kwargs["idempotency_key"] == keys[1]


def test_a_retry_of_a_pending_off_session_attempt_reuses_its_key():
    _lead, plan, a = _owing_trip()
    pending = plan.record_charge(kind=Charge.Kind.TRIP, amount=Decimal("222.00"), reservation=a)
    create_p, retrieve_p = _stripe()

    with create_p as create, retrieve_p:
        charge = trip_money.charge_trip(a, Decimal("222.00"))

    assert charge.pk == pending.pk
    assert create.call_args.kwargs["idempotency_key"] == pending.idempotency_key
    assert plan.charges.filter(kind=Charge.Kind.TRIP).count() == 1


def test_the_webhook_reconciles_a_trip_success_once():
    lead, plan, a = _owing_trip()
    create_p, retrieve_p = _stripe()
    with create_p, retrieve_p:
        charge = trip_money.charge_trip(a, Decimal("222.00"))
        webhooks.process_stripe_event(
            {
                "type": "payment_intent.succeeded",
                "data": {
                    "object": {
                        "id": "pi_trip",
                        "metadata": {"lead_id": str(lead.pk), "kind": "trip"},
                    }
                },
            }
        )

    assert JournalEntry.objects.filter(charge=charge).count() == 1
    assert ledger.order_balances(lead)["collected"] == Decimal("1739.00")


def test_the_webhook_marks_a_trip_failure_without_failing_the_balance():
    lead, plan, a = _owing_trip()
    plan.balance_status = PaymentPlan.BalanceStatus.PAID
    plan.save(update_fields=["balance_status"])
    pending = plan.record_charge(kind=Charge.Kind.TRIP, amount=Decimal("222.00"), reservation=a)
    pending.stripe_payment_intent_id = "pi_fail"
    pending.save(update_fields=["stripe_payment_intent_id"])

    webhooks.process_stripe_event(
        {
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_fail",
                    "metadata": {"lead_id": str(lead.pk), "kind": "trip"},
                    "last_payment_error": {"message": "Insufficient funds"},
                }
            },
        }
    )

    pending.refresh_from_db()
    plan.refresh_from_db()
    assert pending.status == Charge.Status.FAILED
    assert plan.balance_status == PaymentPlan.BalanceStatus.PAID
    assert Notification.objects.filter(lead=lead, title="Trip charge failed").exists()


def test_reconcile_finishes_a_trip_charge_whose_webhook_never_came():
    lead, plan, a = _owing_trip()
    pending = plan.record_charge(kind=Charge.Kind.TRIP, amount=Decimal("222.00"), reservation=a)
    pending.stripe_payment_intent_id = "pi_trip"
    pending.save(update_fields=["stripe_payment_intent_id"])
    Charge.objects.filter(pk=pending.pk).update(updated_at=timezone.now() - timedelta(hours=1))
    create_p, retrieve_p = _stripe()

    with create_p, retrieve_p:
        assert payment_tasks.reconcile_open_charges() == 1

    pending.refresh_from_db()
    assert pending.status == Charge.Status.SUCCEEDED
    assert trip_money.money_for(a).difference == Decimal("0.00")


def test_open_trip_intent_reuses_the_open_intent_for_that_trip_only():
    lead, plan, (a, b) = _two_trips()
    _collect(lead, "1517.00", "full")
    _card(plan)  # a Stripe customer already exists, so no Customer.create round-trip
    intents = [_intent("pi_a"), _intent("pi_b")]
    with patch.object(stripe.PaymentIntent, "create", side_effect=intents) as create:
        ca, secret_a = trip_money.open_trip_intent(a, Decimal("50.00"))
        cb, secret_b = trip_money.open_trip_intent(b, Decimal("50.00"))
        ca2, secret_a2 = trip_money.open_trip_intent(a, Decimal("50.00"))

    assert create.call_count == 2
    assert (ca.reservation, cb.reservation) == (a, b)
    assert ca2.pk == ca.pk and secret_a2 == secret_a and secret_b != secret_a
    assert create.call_args_list[0].kwargs["metadata"]["reservation_id"] == str(a.pk)


def test_a_trip_charge_closes_that_trips_overtime_invoiced_task_only():
    lead, _plan, a = _owing_trip()
    b = lead.reservations.exclude(pk=a.pk).get()
    now = timezone.now()
    mine = Task.objects.create(
        lead=lead, reservation=a, kind="overtime_invoiced", status=Task.Status.OPEN, opens_at=now
    )
    other = Task.objects.create(
        lead=lead, reservation=b, kind="overtime_invoiced", status=Task.Status.OPEN, opens_at=now
    )
    create_p, retrieve_p = _stripe()

    with create_p, retrieve_p:
        trip_money.charge_trip(a, Decimal("222.00"))

    mine.refresh_from_db()
    other.refresh_from_db()
    assert mine.status == Task.Status.DONE
    assert other.status == Task.Status.OPEN
