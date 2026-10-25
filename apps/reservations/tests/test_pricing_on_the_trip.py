"""Overtime and extras live on the trip (spec 2026-10-25 §3, APC-100).

`line_total` is the one truth for the order page, the receipt and the invoice line, so
overtime hours and flat extras are stored on the Reservation and folded into it here.
"""

from decimal import Decimal

import pytest

from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import TripExtra

pytestmark = pytest.mark.django_db


def _hourly(**kw):
    return ReservationFactory(trip_type="hourly", rate=Decimal("185"), hours=Decimal("6"), **kw)


# --- overtime ---------------------------------------------------------------
def test_overtime_bills_at_the_discounted_rate():
    r = _hourly(overtime_hours=Decimal("2"), discount_pct=Decimal("10"))

    assert r.discounted_rate == Decimal("166.50")
    assert r.overtime_amount == Decimal("333.00")


def test_percent_gratuity_covers_base_and_overtime():
    r = _hourly(overtime_hours=Decimal("2"), gratuity_pct=Decimal("20"))

    assert r.gratuity == Decimal("296.00")  # (1110 + 370) × 20%
    assert r.overtime_gratuity == Decimal("74.00")
    assert r.line_total == Decimal("1776.00")


def test_gratuity_is_the_sum_of_its_two_cent_rounded_parts():
    """1 h at 100.01 and 15% → base 15.00 (15.0015) + overtime 15.00; a single rounding of
    the sum would give 30.00 too, but 0.5% cases differ by a cent. Pin the decomposition."""
    r = ReservationFactory(
        trip_type="hourly",
        rate=Decimal("33.33"),
        hours=Decimal("1"),
        overtime_hours=Decimal("1"),
        gratuity_pct=Decimal("5"),
    )
    base = (Decimal("33.33") * 5 / 100).quantize(Decimal("0.01"))  # 1.67 (1.6665)

    assert r.overtime_gratuity == base
    assert r.gratuity == base + r.overtime_gratuity  # 3.34, not 3.33


def test_a_flat_gratuity_adds_nothing_on_overtime():
    r = _hourly(overtime_hours=Decimal("2"), gratuity_flat=Decimal("100"))

    assert r.gratuity == Decimal("100.00")
    assert r.overtime_gratuity == Decimal("0.00")


# --- extras -----------------------------------------------------------------
def test_extras_add_and_a_credit_subtracts():
    r = _hourly()
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.CLEANING, label="Cleaning fee", amount=Decimal("250")
    )
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.CREDIT, label="Courtesy", amount=Decimal("50")
    )

    assert r.extras_total == Decimal("200.00")
    assert r.line_total == Decimal("1310.00")


def test_a_credit_larger_than_the_trip_floors_the_total_at_zero():
    r = _hourly()
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.CREDIT, amount=Decimal("5000"))

    assert r.line_total == Decimal("0.00")


def test_extras_do_not_earn_gratuity():
    r = _hourly(gratuity_pct=Decimal("20"))
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.TOLLS, amount=Decimal("40"))

    assert r.gratuity == Decimal("222.00")


def test_an_extra_gets_its_kind_label_when_none_is_given():
    assert TripExtra.Kind.default_label(TripExtra.Kind.CLEANING) == "Cleaning fee"
    assert TripExtra.Kind.default_label(TripExtra.Kind.CREDIT) == "Credit"


# --- what the customer sees -------------------------------------------------
def test_the_lines_still_sum_to_the_line_total():
    r = _hourly(
        overtime_hours=Decimal("1"), gratuity_pct=Decimal("20"), discount_flat=Decimal("10")
    )
    TripExtraFactory(
        reservation=r, kind=TripExtra.Kind.PARKING, label="Parking", amount=Decimal("15")
    )
    TripExtraFactory(reservation=r, kind=TripExtra.Kind.CREDIT, label="Credit", amount=Decimal("5"))

    lines = r.price_lines()

    assert sum(line.amount for line in lines) == r.line_total
    labels = [line.label for line in lines]
    assert "Overtime (1 hr)" in labels
    assert "Parking" in labels
    assert [line for line in lines if line.label == "Credit"][0].amount == Decimal("-5.00")


def test_a_transfer_names_its_overtime_wait_time():
    r = ReservationFactory(
        trip_type="transfer", rate=Decimal("185"), hours=Decimal("1"), overtime_hours=Decimal("2")
    )

    assert "Wait time (2 hrs)" in [line.label for line in r.price_lines()]


def test_overtime_or_an_extra_counts_as_extras_for_the_customer_copy():
    assert _hourly(overtime_hours=Decimal("1")).has_extras is True
    r = _hourly()
    TripExtraFactory(reservation=r)
    assert r.has_extras is True


def test_a_plain_price_still_has_no_extras():
    assert _hourly().has_extras is False


# --- booked total -----------------------------------------------------------
def test_booked_total_is_null_until_frozen():
    assert _hourly().booked_total is None


# --- the back-fill migration ------------------------------------------------
def _backfill_module():
    import importlib

    return importlib.import_module("apps.reservations.migrations.0024_backfill_booked_totals")


def test_backfill_freezes_booked_totals_on_booked_orders_only():
    from apps.leads.models import Lead
    from apps.reservations.models import Reservation

    booked = _hourly(gratuity_pct=Decimal("20"))
    booked.lead.status = Lead.Status.BOOKED
    booked.lead.save(update_fields=["status"])
    second = ReservationFactory(lead=booked.lead, rate=Decimal("185"), hours=Decimal("1"))
    quoted = _hourly()

    frozen = _backfill_module().freeze_booked_totals(Reservation)

    for r in (booked, second, quoted):
        r.refresh_from_db()
    assert frozen == 2
    assert booked.booked_total == Decimal("1332.00")  # 6 × 185 × 1.2
    assert second.booked_total == Decimal("185.00")
    assert quoted.booked_total is None


def test_backfill_moves_a_charged_final_overtime_onto_the_trips(caplog):
    from django.utils import timezone

    from apps.leads.models import Lead
    from apps.payments.factories import ChargeFactory, PaymentPlanFactory
    from apps.payments.models import Charge
    from apps.reservations.models import Reservation, TripReview

    trip = _hourly(gratuity_pct=Decimal("20"))
    trip.lead.status = Lead.Status.BOOKED
    trip.lead.save(update_fields=["status"])
    other = ReservationFactory(lead=trip.lead, rate=Decimal("185"), hours=Decimal("1"))
    TripReview.objects.create(
        reservation=trip, billable_overtime_minutes=120, completed_at=timezone.now()
    )
    TripReview.objects.create(
        reservation=other,
        billable_overtime_minutes=30,
        overtime_waived=True,
        completed_at=timezone.now(),
    )
    plan = PaymentPlanFactory(lead=trip.lead)
    ChargeFactory(
        plan=plan,
        kind=Charge.Kind.FINAL,
        status=Charge.Status.SUCCEEDED,
        amount=Decimal("444.00"),
        overtime_amount=Decimal("370.00"),
    )

    module = _backfill_module()
    module.freeze_booked_totals(Reservation)
    moved = module.move_final_overtime_onto_trips(Reservation, Charge, TripReview)

    trip.refresh_from_db()
    other.refresh_from_db()
    assert moved == 1
    assert trip.booked_total == Decimal("1332.00")  # frozen before the overtime landed
    assert trip.overtime_hours == Decimal("2.00") and trip.line_total == Decimal("1776.00")
    assert other.overtime_hours == Decimal("0.00")
    assert "billed overtime" not in caplog.text


def test_backfill_logs_an_order_whose_split_does_not_add_up(caplog):
    from django.utils import timezone

    from apps.payments.factories import ChargeFactory, PaymentPlanFactory
    from apps.payments.models import Charge
    from apps.reservations.models import Reservation, TripReview

    trip = _hourly()
    TripReview.objects.create(
        reservation=trip, billable_overtime_minutes=60, completed_at=timezone.now()
    )
    ChargeFactory(
        plan=PaymentPlanFactory(lead=trip.lead),
        kind=Charge.Kind.FINAL,
        status=Charge.Status.SUCCEEDED,
        amount=Decimal("999"),
        overtime_amount=Decimal("999"),
    )

    moved = _backfill_module().move_final_overtime_onto_trips(Reservation, Charge, TripReview)

    trip.refresh_from_db()
    assert moved == 0 and trip.overtime_hours == Decimal("0.00")
    assert "billed overtime 999.00 but the trips would carry 185.00" in caplog.text


def test_backfill_leaves_an_inexact_fifty_minute_split_alone(caplog):
    """50 min at 185/hr was charged as 154.17; 0.83 h × 185 = 153.55. Not exact → stays 0."""
    from django.utils import timezone

    from apps.payments.factories import ChargeFactory, PaymentPlanFactory
    from apps.payments.models import Charge
    from apps.reservations.models import Reservation, TripReview

    trip = _hourly()
    TripReview.objects.create(
        reservation=trip, billable_overtime_minutes=50, completed_at=timezone.now()
    )
    ChargeFactory(
        plan=PaymentPlanFactory(lead=trip.lead),
        kind=Charge.Kind.FINAL,
        status=Charge.Status.SUCCEEDED,
        amount=Decimal("154.17"),
        overtime_amount=Decimal("154.17"),
    )

    _backfill_module().move_final_overtime_onto_trips(Reservation, Charge, TripReview)

    trip.refresh_from_db()
    assert trip.overtime_hours == Decimal("0.00")
    assert "left at 0 hours" in caplog.text
