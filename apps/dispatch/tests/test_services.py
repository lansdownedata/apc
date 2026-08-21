from decimal import Decimal

import pytest

from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _booked_trip(**kwargs):
    """A trip on a sold order — the only kind that may legally be farmed out."""
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    return ReservationFactory(**kwargs)


def test_send_offer_creates_an_offered_assignment():
    res, vendor = _booked_trip(), VendorFactory()
    a = services.send_offer(res, vendor, payout=Decimal("140.00"))
    assert a.status == Assignment.Status.OFFERED
    assert a.resolved_at is None
    assert services.active_assignment(res) == a


def test_assign_direct_skips_the_offer_and_confirms():
    res, vendor = _booked_trip(), VendorFactory()
    a = services.assign_direct(res, vendor, payout=Decimal("140.00"), note="arranged by phone")
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at is not None
    assert a.note == "arranged by phone"


def test_a_second_offer_while_one_is_active_is_refused():
    res, vendor = _booked_trip(), VendorFactory()
    services.send_offer(res, vendor, payout=Decimal("140.00"))
    with pytest.raises(services.AssignmentError):
        services.send_offer(res, VendorFactory(), payout=Decimal("150.00"))


def test_declining_frees_the_trip_for_a_new_offer():
    res = _booked_trip()
    first = services.send_offer(res, VendorFactory(), payout=Decimal("140.00"))
    services.decline(first)
    first.refresh_from_db()
    assert first.status == Assignment.Status.DECLINED
    assert first.resolved_at is not None
    assert services.active_assignment(res) is None

    second = services.send_offer(res, VendorFactory(), payout=Decimal("150.00"))
    assert services.active_assignment(res) == second
    assert res.assignments.count() == 2  # history is kept, not overwritten


def test_withdrawing_a_confirmed_assignment_records_the_reason():
    a = AssignmentFactory(status=Assignment.Status.CONFIRMED)
    services.withdraw(a, note="vendor cancelled")
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN
    assert a.note == "vendor cancelled"


def test_confirming_a_resolved_assignment_is_refused():
    a = AssignmentFactory(status=Assignment.Status.DECLINED)
    with pytest.raises(services.AssignmentError):
        services.confirm(a)


def test_confirm_moves_an_offer_to_confirmed():
    a = AssignmentFactory(status=Assignment.Status.OFFERED)
    services.confirm(a)
    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at is not None


def test_a_trip_on_an_unsold_quote_cannot_be_farmed_out():
    """Farming out a quote that was never sold emails a real affiliate a trip sheet for a
    trip nobody bought — the one failure mode that reaches outside the building."""
    res = _booked_trip(lead=LeadFactory(status=Lead.Status.QUOTED))
    with pytest.raises(services.AssignmentError):
        services.send_offer(res, VendorFactory(), payout=Decimal("140.00"))


def test_a_cancelled_trip_cannot_be_farmed_out():
    res = _booked_trip(trip_status=Reservation.TripStatus.CANCELLED)
    with pytest.raises(services.AssignmentError):
        services.assign_direct(res, VendorFactory(), payout=Decimal("140.00"))


def test_release_trips_withdraws_active_coverage():
    res = _booked_trip()
    offered = services.send_offer(res, VendorFactory(), payout=Decimal("140.00"))
    services.release_trips([res], note="Order cancelled")
    offered.refresh_from_db()
    assert offered.status == Assignment.Status.WITHDRAWN
    assert offered.note == "Order cancelled"


def test_release_trips_leaves_resolved_history_alone():
    res = _booked_trip()
    declined = services.decline(
        services.send_offer(res, VendorFactory(), payout=Decimal("140.00")), note="no cars"
    )
    services.release_trips([res], note="Order cancelled")
    declined.refresh_from_db()
    assert declined.status == Assignment.Status.DECLINED
    assert declined.note == "no cars"


def test_confirming_an_already_confirmed_assignment_is_a_no_op():
    a = AssignmentFactory(status=Assignment.Status.CONFIRMED)
    first_resolved = a.resolved_at
    assert services.confirm(a) is a
    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at == first_resolved
