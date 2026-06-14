from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory
from apps.payments import ledger
from apps.payments.models import JournalEntry
from apps.payments.tasks import recognize_due_revenue
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def test_recognition_draws_from_deferred():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("2670.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    bals = ledger.order_balances(lead)
    assert bals["recognized"] == Decimal("1500.00")
    assert bals["deferred"] == Decimal("1170.00")
    assert bals["ar"] == Decimal("0.00")
    res.refresh_from_db()
    assert res.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    assert res.recognized_amount == Decimal("1500.00")
    assert res.recognized_at is not None


def test_recognition_overflows_to_ar_when_underpaid():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    bals = ledger.order_balances(lead)
    assert bals["recognized"] == Decimal("1500.00")
    assert bals["deferred"] == Decimal("0.00")
    assert bals["ar"] == Decimal("165.00")


def test_recognition_is_idempotent():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead, amount=Decimal("1500.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    ledger.recognize_reservation(res)
    assert (
        JournalEntry.objects.filter(
            reservation=res, kind=JournalEntry.Kind.REVENUE_RECOGNIZED
        ).count()
        == 1
    )


def test_recognition_skips_zero_value_trip():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, base_rate=Decimal("0.00"))
    result = ledger.recognize_reservation(res)
    assert result is None
    assert JournalEntry.objects.filter(reservation=res).count() == 0


def test_recognize_due_revenue_only_earned_past_trips():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead, amount=Decimal("3000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED, idempotency_key="cap1",
    )
    past = date.today() - timedelta(days=1)
    future = date.today() + timedelta(days=10)
    done_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("1000.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.DONE,
    )
    noshow_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("500.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.NO_SHOW,
    )
    done_future = TransferReservationFactory(
        lead=lead, base_rate=Decimal("700.00"),
        pickup_date=future, trip_status=Reservation.TripStatus.DONE,
    )
    cancelled_past = TransferReservationFactory(
        lead=lead, base_rate=Decimal("400.00"),
        pickup_date=past, trip_status=Reservation.TripStatus.CANCELLED,
    )

    assert recognize_due_revenue() == 2  # done_past + noshow_past only

    for res in (done_past, noshow_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    for res in (done_future, cancelled_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.DEFERRED

    assert recognize_due_revenue() == 0  # idempotent second run
