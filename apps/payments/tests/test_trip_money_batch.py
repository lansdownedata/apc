"""`trip_money.allocations`: every order's prepaid shares in a fixed number of queries, by
the same rule as one order's `allocation` (a customer with years of history exports them
all at once)."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import trip_money
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import Charge
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview

pytestmark = pytest.mark.django_db

S = Reservation.TripStatus


def _order(*statuses: str, prepaid: str = "300.00", plan: bool = True) -> Lead:
    lead = LeadFactory(status=Lead.Status.BOOKED)
    for i, status in enumerate(statuses):
        ReservationFactory(
            lead=lead,
            trip_status=status,
            booked_total=Decimal("185.00"),
            pickup_date=date(2026, 12, 1) + timedelta(days=i),
            pickup_time=time(9, 0),
        )
    if plan:
        made = PaymentPlanFactory(lead=lead, quote_total=Decimal("600.00"))
        ChargeFactory(
            plan=made,
            kind=Charge.Kind.DEPOSIT,
            amount=Decimal(prepaid),
            status=Charge.Status.SUCCEEDED,
        )
    return lead


def _orders() -> list[Lead]:
    """Orders covering each branch of the rule: no plan, a plain cancellation that takes
    nothing, an order-level refund, a pending charge that does not count, a trip-level
    refund that leaves the pool alone, a settled trip's frozen share, and no trips."""
    no_plan = _order(S.DONE, S.DONE, plan=False)
    cancelled_first = _order(S.CANCELLED, S.DONE, S.DONE)
    refunded = _order(S.DONE, S.DONE)
    ChargeFactory(
        plan=refunded.payment,
        kind=Charge.Kind.REFUND,
        amount=Decimal("50.00"),
        status=Charge.Status.SUCCEEDED,
    )
    ChargeFactory(
        plan=refunded.payment,
        kind=Charge.Kind.BALANCE,
        amount=Decimal("300.00"),
        status=Charge.Status.PENDING,
    )
    trip_refund = _order(S.DONE, S.DONE)
    ChargeFactory(
        plan=trip_refund.payment,
        kind=Charge.Kind.REFUND,
        reservation=trip_refund.reservations.first(),
        amount=Decimal("40.00"),
        status=Charge.Status.SUCCEEDED,
    )
    settled = _order(S.DONE, S.DONE, S.LATE_CANCEL)
    last = settled.reservations.order_by("-pickup_date").first()
    TripReview.objects.create(
        reservation=last, processed_at=timezone.now(), prepaid_share=Decimal("120.00")
    )
    empty = _order()
    return [no_plan, cancelled_first, refunded, trip_refund, settled, empty]


def test_allocations_match_each_orders_allocation():
    leads = _orders()
    batched = trip_money.allocations(leads)
    assert set(batched) == {lead.pk for lead in leads}
    for lead in leads:
        assert batched[lead.pk] == trip_money.allocation(lead)
    # Not vacuous: the cancelled trip took nothing and the settled one kept its share.
    cancelled_first, settled = leads[1], leads[4]
    assert sorted(batched[cancelled_first.pk].values()) == [
        Decimal("0.00"),
        Decimal("115.00"),
        Decimal("185.00"),
    ]
    assert Decimal("120.00") in batched[settled.pk].values()


def test_allocations_cost_the_same_for_any_number_of_orders(django_assert_num_queries):
    leads = _orders()
    # Succeeded order-level charges, settled trips' frozen shares, the orders' trips.
    with django_assert_num_queries(3):
        trip_money.allocations(leads[:2])
    with django_assert_num_queries(3):
        trip_money.allocations(leads)


def test_no_orders_cost_nothing(django_assert_num_queries):
    with django_assert_num_queries(0):
        assert trip_money.allocations([]) == {}
