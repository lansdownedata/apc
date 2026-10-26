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
    res = TransferReservationFactory(lead=lead, rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead,
        amount=Decimal("2670.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap1",
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
    res = TransferReservationFactory(lead=lead, rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1335.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap1",
    )
    ledger.recognize_reservation(res)
    bals = ledger.order_balances(lead)
    assert bals["recognized"] == Decimal("1500.00")
    assert bals["deferred"] == Decimal("0.00")
    assert bals["ar"] == Decimal("165.00")


def test_recognition_is_idempotent():
    lead = LeadFactory()
    res = TransferReservationFactory(lead=lead, rate=Decimal("1500.00"))
    ledger.post_capture(
        lead=lead,
        amount=Decimal("1500.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap1",
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
    res = TransferReservationFactory(lead=lead, rate=Decimal("0.00"))
    result = ledger.recognize_reservation(res)
    assert result is None
    assert JournalEntry.objects.filter(reservation=res).count() == 0


def test_recognize_due_revenue_only_earned_past_trips():
    lead = LeadFactory()
    ledger.post_capture(
        lead=lead,
        amount=Decimal("3000.00"),
        kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
        idempotency_key="cap1",
    )
    past = date.today() - timedelta(days=1)
    future = date.today() + timedelta(days=10)
    done_past = TransferReservationFactory(
        lead=lead,
        rate=Decimal("1000.00"),
        pickup_date=past,
        trip_status=Reservation.TripStatus.DONE,
    )
    noshow_past = TransferReservationFactory(
        lead=lead,
        rate=Decimal("500.00"),
        pickup_date=past,
        trip_status=Reservation.TripStatus.NO_SHOW,
    )
    done_future = TransferReservationFactory(
        lead=lead,
        rate=Decimal("700.00"),
        pickup_date=future,
        trip_status=Reservation.TripStatus.DONE,
    )
    cancelled_past = TransferReservationFactory(
        lead=lead,
        rate=Decimal("400.00"),
        pickup_date=past,
        trip_status=Reservation.TripStatus.CANCELLED,
    )

    assert recognize_due_revenue() == 2  # done_past + noshow_past only

    for res in (done_past, noshow_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.RECOGNIZED
    for res in (done_future, cancelled_past):
        res.refresh_from_db()
        assert res.revenue_status == Reservation.RevenueStatus.DEFERRED

    assert recognize_due_revenue() == 0  # idempotent second run


# --- APC-101: recognised revenue follows the trip's live total --------------------------
from apps.core.choices import Account  # noqa: E402
from apps.payments.factories import ChargeFactory, PaymentPlanFactory  # noqa: E402
from apps.payments.models import Charge  # noqa: E402
from apps.reservations.factories import ReservationFactory, TripExtraFactory  # noqa: E402


def _earned_trip(**kw):
    """A done hourly trip: 3 h × 200 + 20% = 720; fully paid."""
    lead = LeadFactory()
    kw.setdefault("trip_status", Reservation.TripStatus.DONE)
    trip = ReservationFactory(
        lead=lead,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        gratuity_pct=Decimal("20"),
        **kw,
    )
    PaymentPlanFactory(lead=lead, quote_total=trip.line_total)
    ledger.post_capture(
        lead=lead,
        amount=trip.line_total,
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="paid",
    )
    return lead, trip


def _bal(lead):
    return ledger.order_balances(lead)


def test_recognition_includes_overtime_and_extras_again():
    lead, trip = _earned_trip(overtime_hours=Decimal("1"))
    TripExtraFactory(reservation=trip, amount=Decimal("40"))
    trip.refresh_from_db()

    ledger.recognize_reservation(trip)

    assert _bal(lead)["recognized"] == Decimal("1000.00")  # 960 + 40
    trip.refresh_from_db()
    assert trip.recognized_amount == Decimal("1000.00")


def test_true_up_recognizes_a_late_cancelled_trip():
    lead, trip = _earned_trip(trip_status=Reservation.TripStatus.LATE_CANCEL)

    ledger.true_up_recognition(trip)

    assert _bal(lead)["recognized"] == Decimal("720.00")


def test_true_up_does_nothing_for_a_plain_cancelled_trip():
    lead, trip = _earned_trip(trip_status=Reservation.TripStatus.CANCELLED)

    assert ledger.true_up_recognition(trip) is None
    assert _bal(lead)["recognized"] == Decimal("0.00")


def test_true_up_books_an_added_charge_as_revenue_once():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)  # 720
    trip.overtime_hours = Decimal("1")  # +240
    trip.save(update_fields=["overtime_hours"])
    charge = ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.TRIP,
        reservation=trip,
        amount=Decimal("240.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ledger.post_capture(
        lead=lead,
        amount=Decimal("240.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="trip-charge",
        charge=charge,
        reservation=trip,
    )

    entry = ledger.true_up_recognition(trip)

    assert entry.kind == JournalEntry.Kind.ADJUSTMENT and entry.is_balanced
    bals = _bal(lead)
    assert (bals["recognized"], bals["ar"], bals["deferred"]) == (
        Decimal("960.00"),
        Decimal("0.00"),
        Decimal("0.00"),
    )
    trip.refresh_from_db()
    assert trip.recognized_amount == Decimal("960.00")


def test_true_up_after_a_trip_refund_does_not_double_reduce():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)
    TripExtraFactory(
        reservation=trip, kind="credit", label="Refund: courtesy", amount=Decimal("100")
    )
    refund = ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.REFUND,
        reservation=trip,
        amount=Decimal("100.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ledger.post_refund(lead=lead, amount=Decimal("100.00"), idempotency_key="re", charge=refund)
    trip.refresh_from_db()

    assert ledger.true_up_recognition(trip) is None
    bals = _bal(lead)
    assert (bals["recognized"], bals["refunded"]) == (Decimal("720.00"), Decimal("100.00"))


def test_true_up_reverses_a_lowered_invoiced_trip_into_ar():
    lead = LeadFactory()
    trip = ReservationFactory(
        lead=lead,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        gratuity_pct=Decimal("20"),
        trip_status=Reservation.TripStatus.DONE,
    )
    ledger.recognize_reservation(trip)  # nothing collected: 720 sits in A/R
    TripExtraFactory(reservation=trip, kind="credit", label="Credit", amount=Decimal("100"))
    trip.refresh_from_db()

    entry = ledger.true_up_recognition(trip)

    assert entry.is_balanced
    bals = _bal(lead)
    assert (bals["recognized"], bals["ar"], bals["refunded"]) == (
        Decimal("620.00"),
        Decimal("620.00"),
        Decimal("0.00"),
    )


def test_true_up_is_idempotent_at_the_same_total():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])

    ledger.true_up_recognition(trip)
    ledger.true_up_recognition(trip)

    assert (
        JournalEntry.objects.filter(reservation=trip, kind=JournalEntry.Kind.ADJUSTMENT).count()
        == 1
    )
    assert _bal(lead)["recognized"] == Decimal("960.00")


def test_a_trip_whose_overtime_a_historical_final_charge_booked_is_not_booked_again():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)  # 720, before the overtime existed
    # A historical final bill then booked 240 of overtime straight to revenue and the
    # back-fill put the hour on the trip.
    ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.FINAL,
        amount=Decimal("240.00"),
        status=Charge.Status.SUCCEEDED,
        overtime_amount=Decimal("240.00"),
        overtime_gratuity=Decimal("40.00"),
    )
    ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.OVERTIME_CAPTURED,
        idempotency_key="ot",
        lines=[
            (Account.CASH, Decimal("240.00"), Decimal("0.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("240.00")),
        ],
    )
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])

    assert ledger.true_up_recognition(trip) is None
    assert _bal(lead)["recognized"] == Decimal("960.00")


def test_a_trip_refund_hits_refunds_not_another_trips_deferred_cash():
    """Two trips prepaid in full; A recognised, B still deferred. Refunding A must not draw
    down B's deferred money."""
    lead = LeadFactory()
    a = ReservationFactory(
        lead=lead,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        trip_status=Reservation.TripStatus.DONE,
    )
    ReservationFactory(lead=lead, rate=Decimal("300"), hours=1, min_hours=0)
    plan = PaymentPlanFactory(lead=lead, quote_total=Decimal("900.00"))
    ledger.post_capture(
        lead=lead,
        amount=Decimal("900.00"),
        kind=JournalEntry.Kind.BALANCE_CAPTURED,
        idempotency_key="paid",
    )
    ledger.recognize_reservation(a)
    a.refresh_from_db()
    refund = ChargeFactory(
        plan=plan,
        kind=Charge.Kind.REFUND,
        reservation=a,
        amount=Decimal("100.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ledger.post_trip_refund(
        lead=lead, reservation=a, amount=Decimal("100.00"), idempotency_key="re", charge=refund
    )

    bals = _bal(lead)
    assert (bals["deferred"], bals["refunded"], bals["recognized"]) == (
        Decimal("300.00"),
        Decimal("100.00"),
        Decimal("600.00"),
    )


def test_refund_then_edit_then_true_up_twice_posts_one_adjustment():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)  # 720
    trip.refresh_from_db()
    TripExtraFactory(
        reservation=trip, kind="credit", label="Refund: courtesy", amount=Decimal("100")
    )
    refund = ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.REFUND,
        reservation=trip,
        amount=Decimal("100.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ledger.post_trip_refund(
        lead=lead, reservation=trip, amount=Decimal("100.00"), idempotency_key="re", charge=refund
    )
    TripExtraFactory(reservation=trip, kind="tolls", amount=Decimal("40"))
    trip.refresh_from_db()

    first = ledger.true_up_recognition(trip)
    second = ledger.true_up_recognition(trip)

    assert first is not None and second is None
    assert (
        JournalEntry.objects.filter(reservation=trip, kind=JournalEntry.Kind.ADJUSTMENT).count()
        == 1
    )
    assert _bal(lead)["recognized"] == Decimal("760.00")  # 720 + 40, the refund stays contra


def test_true_up_round_trip_posts_every_adjustment():
    lead, trip = _earned_trip()
    ledger.recognize_reservation(trip)
    for hours in ("1", "0", "1"):
        trip.overtime_hours = Decimal(hours)
        trip.save(update_fields=["overtime_hours"])
        trip.refresh_from_db()
        ledger.true_up_recognition(trip)

    assert (
        JournalEntry.objects.filter(reservation=trip, kind=JournalEntry.Kind.ADJUSTMENT).count()
        == 3
    )
    assert _bal(lead)["recognized"] == Decimal("960.00")


def test_the_cron_does_not_rebook_a_historical_finals_overtime():
    lead, trip = _earned_trip(pickup_date=date.today() - timedelta(days=3))
    ChargeFactory(
        plan=lead.payment,
        kind=Charge.Kind.FINAL,
        amount=Decimal("240.00"),
        status=Charge.Status.SUCCEEDED,
        overtime_amount=Decimal("240.00"),
        overtime_gratuity=Decimal("40.00"),
    )
    ledger.post_entry(
        lead=lead,
        kind=JournalEntry.Kind.OVERTIME_CAPTURED,
        idempotency_key="ot",
        lines=[
            (Account.CASH, Decimal("240.00"), Decimal("0.00")),
            (Account.RECOGNIZED_REVENUE, Decimal("0.00"), Decimal("240.00")),
        ],
    )
    trip.overtime_hours = Decimal("1")
    trip.save(update_fields=["overtime_hours"])

    recognize_due_revenue()

    assert _bal(lead)["recognized"] == Decimal("960.00")
