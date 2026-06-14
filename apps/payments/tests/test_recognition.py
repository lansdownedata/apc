from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory
from apps.payments import ledger
from apps.payments.models import JournalEntry
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
