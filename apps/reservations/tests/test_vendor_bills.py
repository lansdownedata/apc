"""APC-104 — Needs vendor bill: processed farmed-out trips whose payable has no invoice yet,
grouped by vendor (spec 2026-10-25 §6). One bill per trip; the row's form posts to the
existing `payable_invoice` endpoint, and the trip leaves the view once the invoice is in."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import payables
from apps.payments.models import AffiliatePayable
from apps.reservations import processing
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db
S = Reservation.TripStatus


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


@pytest.fixture
def accountant():
    return UserFactory(can_manage_payments=True)


def _farmed(
    vendor=None,
    *,
    days_ago=1,
    processed=True,
    customer="Dana Whitfield",
    lead=None,
    status=S.DONE,
    payout="420",
):
    """A finished trip farmed out to `vendor`, processed, with its payable (no invoice)."""
    lead = lead or LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(name=customer))
    trip = ReservationFactory(
        lead=lead,
        trip_status=status,
        trip_type="hourly",
        rate=Decimal("200"),
        hours=Decimal("3"),
        min_hours=0,
        pickup_date=timezone.localdate() - timedelta(days=days_ago),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
    )
    AssignmentFactory(
        reservation=trip,
        status=Assignment.Status.CONFIRMED,
        vendor=vendor or VendorFactory(),
        payout=Decimal(payout),
    )
    payables.ensure_payable(trip)
    TripReview.objects.update_or_create(
        reservation=trip, defaults={"processed_at": timezone.now() if processed else None}
    )
    return trip


def _payable(trip) -> AffiliatePayable:
    return AffiliatePayable.objects.get(assignment__reservation=trip)


def _groups(**query):
    filters = processing.Filters.from_query({"view": "bills", **query})
    return processing.vendor_bills(filters, query.get("sort", "oldest"))


# --- what is listed ------------------------------------------------------------------


def test_lists_a_processed_farmed_out_trip_with_no_invoice_under_its_vendor():
    vendor = VendorFactory(name="Capital Coach")
    older = _farmed(vendor, days_ago=5)
    newer = _farmed(vendor, days_ago=2, payout="300")

    [group] = _groups()

    assert group.vendor == vendor
    assert [t.pk for t in group.trips] == [older.pk, newer.pk]
    assert group.count == 2
    assert group.expected_total == Decimal("720.00")
    assert group.oldest == older.pickup_date
    assert group.trips[0].payable == _payable(older)
    assert group.trips[0].route_short is not None


def test_leaves_out_what_is_not_waiting_for_a_vendor_bill(accountant):
    vendor = VendorFactory()
    _farmed(vendor, processed=False)  # not processed yet
    awaiting = _farmed(vendor)
    payables.record_invoice(_payable(awaiting), user=accountant, number="7", amount=Decimal("420"))
    approved = _farmed(vendor)
    payables.record_invoice(_payable(approved), user=accountant, number="8", amount=Decimal("420"))
    payables.approve(_payable(approved), user=accountant)
    in_house = ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        trip_status=S.DONE,
        pickup_date=timezone.localdate() - timedelta(days=1),
    )
    AssignmentFactory(reservation=in_house, in_house=True)
    TripReview.objects.create(reservation=in_house, processed_at=timezone.now())
    replaced = _farmed(vendor)
    Assignment.objects.filter(reservation=replaced).update(status=Assignment.Status.WITHDRAWN)
    lost = _farmed(vendor)
    Lead.objects.filter(pk=lost.lead_id).update(status=Lead.Status.LOST)

    assert _groups() == []
    assert processing.vendor_bill_count() == 0


def test_a_trip_leaves_once_its_invoice_amount_is_saved(accountant):
    trip = _farmed()
    payables.record_invoice(_payable(trip), user=accountant, number="INV-9", amount=None)
    assert [t.pk for g in _groups() for t in g.trips] == [trip.pk]  # a number alone isn't a bill

    payables.record_invoice(_payable(trip), user=accountant, number="INV-9", amount=Decimal("1"))
    assert _groups() == []


def test_a_reassigned_trip_is_listed_under_the_confirmed_vendor_only():
    first = VendorFactory(name="First Limo")
    second = VendorFactory(name="Second Cars")
    trip = _farmed(first)
    Assignment.objects.filter(reservation=trip).update(status=Assignment.Status.WITHDRAWN)
    AssignmentFactory(
        reservation=trip, status=Assignment.Status.CONFIRMED, vendor=second, payout=Decimal("400")
    )
    payables.ensure_payable(trip)

    [group] = _groups()
    assert group.vendor == second
    assert group.trips[0].payable.assignment.vendor == second
    assert processing.vendor_bill_trip_ids(first) == set()
    assert processing.vendor_bill_trip_ids(second) == {trip.pk}


def test_a_stale_payable_on_a_trip_the_affiliate_dropped_is_not_a_bill():
    trip = _farmed()
    assert _payable(trip)  # made earlier, before LA said the affiliate dropped it
    Reservation.objects.filter(pk=trip.pk).update(trip_status=S.CANCELLED_BY_AFFILIATE)

    assert _groups() == []
    assert processing.vendor_bill_count() == 0


def test_a_processed_trip_whose_payable_was_never_made_is_listed_and_gets_one():
    trip = _farmed()
    AffiliatePayable.objects.filter(assignment__reservation=trip).delete()
    assert processing.vendor_bill_count() == 1

    [group] = _groups()
    assert group.trips[0].payable == _payable(trip)
    assert group.expected_total == Decimal("420.00")


def test_a_payable_from_the_order_level_review_counts_the_same():
    trip = _farmed()
    AffiliatePayable.objects.filter(assignment__reservation=trip).delete()
    payables.ensure_payables(trip.lead)

    assert [t.pk for g in _groups() for t in g.trips] == [trip.pk]


def test_an_unlocked_trip_leaves_and_comes_back_when_processed_again():
    trip = _farmed()
    TripReview.objects.filter(reservation=trip).update(processed_at=None)
    assert _groups() == []

    TripReview.objects.filter(reservation=trip).update(processed_at=timezone.now())
    assert [t.pk for g in _groups() for t in g.trips] == [trip.pk]


def test_the_bills_view_is_a_view():
    assert processing.Filters.from_query({"view": "bills"}).view == "bills"
    assert processing.Filters.from_query({"view": "nope"}).view == "ready"


# --- order, search, dates ----------------------------------------------------------------


def test_vendors_sort_oldest_waiting_first_by_default_then_by_name_or_count():
    a = VendorFactory(name="Alpha Limo")
    b = VendorFactory(name="Bravo Cars")
    _farmed(a, days_ago=2)
    _farmed(b, days_ago=9)
    _farmed(b, days_ago=1)

    assert [g.vendor for g in _groups()] == [b, a]
    assert [g.vendor for g in _groups(sort="vendor")] == [a, b]
    assert [g.vendor for g in _groups(sort="most")] == [b, a]


def test_search_matches_vendor_customer_or_trip_reference():
    a = VendorFactory(name="Alpha Limo")
    b = VendorFactory(name="Bravo Cars")
    ta = _farmed(a, customer="Dana Whitfield")
    tb = _farmed(b, customer="Omar Reyes")

    assert [g.vendor for g in _groups(q="bravo")] == [b]
    assert [g.vendor for g in _groups(q="whitfield")] == [a]
    assert [t.pk for g in _groups(q=tb.reference.lower()) for t in g.trips] == [tb.pk]
    assert ta.pk not in [t.pk for g in _groups(q="omar") for t in g.trips]


def test_dates_bound_the_pickup_date():
    vendor = VendorFactory()
    old = _farmed(vendor, days_ago=20)
    recent = _farmed(vendor, days_ago=2)
    since = (timezone.localdate() - timedelta(days=5)).isoformat()

    [group] = _groups(**{"from": since})
    assert [t.pk for t in group.trips] == [recent.pk]
    until = (timezone.localdate() - timedelta(days=10)).isoformat()
    [group] = _groups(to=until)
    assert [t.pk for t in group.trips] == [old.pk]


def test_count_is_every_waiting_trip():
    _farmed(VendorFactory())
    _farmed(VendorFactory())
    _farmed(VendorFactory(), processed=False)

    assert processing.vendor_bill_count() == 2


def test_query_count_is_flat():
    _farmed(VendorFactory())
    with CaptureQueriesContext(connection) as one:
        _groups()
    for vendor in [VendorFactory() for _ in range(3)]:
        for days in range(1, 7):
            _farmed(vendor, days_ago=days)
    with CaptureQueriesContext(connection) as many:
        groups = _groups()
        [t.route_short for g in groups for t in g.trips]
        [t.lead.contact.name for g in groups for t in g.trips]
    assert len(many) == len(one)
