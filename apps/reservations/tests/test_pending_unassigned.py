"""Trip-status cleanup: quoted trips are Pending, booking moves them to Unassigned."""

from unittest.mock import patch

import pytest

from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import groups
from apps.reservations.drafts import save_reservation_from_draft
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import initial_status

pytestmark = pytest.mark.django_db

PENDING = Reservation.TripStatus.PENDING
UNASSIGNED = Reservation.TripStatus.UNASSIGNED


def _payload():
    return {
        "tripType": "transfer",
        "date": "2026-07-04",
        "time": "15:00",
        "vehicle": "",
        "pax": 3,
        "rate": 185,
        "hours": 1,
        "minHours": 0,
        "stops": [{"address": "Dulles", "note": ""}, {"address": "The Ritz", "note": ""}],
    }


def test_initial_status_follows_the_order():
    assert initial_status(LeadFactory(status=Lead.Status.BOOKED)) == UNASSIGNED
    assert initial_status(LeadFactory(status=Lead.Status.QUOTED)) == PENDING


def test_new_trip_on_a_quote_is_pending():
    lead = LeadFactory(status=Lead.Status.NEW)
    res = save_reservation_from_draft(lead, _payload())
    assert res.trip_status == PENDING


def test_trip_added_to_booked_order_starts_unassigned():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    res = save_reservation_from_draft(lead, _payload())
    res.refresh_from_db()
    assert res.trip_status == UNASSIGNED


def test_book_lead_moves_pending_trips_to_unassigned():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    a = TransferReservationFactory(lead=lead)
    b = TransferReservationFactory(lead=lead)
    blank = TransferReservationFactory(lead=lead, trip_status="")
    cancelled = TransferReservationFactory(lead=lead, trip_status=Reservation.TripStatus.CANCELLED)
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        lead_services.book_lead(lead)
        lead_services.book_lead(lead)  # idempotent: nothing moves twice
    for trip in (a, b, blank):
        trip.refresh_from_db()
        assert trip.trip_status == UNASSIGNED
        events = TripStatusEvent.objects.filter(reservation=trip, status=UNASSIGNED)
        assert events.count() == 1
        assert events.get().source == TripStatusEvent.Source.SYSTEM
    cancelled.refresh_from_db()
    assert cancelled.trip_status == Reservation.TripStatus.CANCELLED
    assert not TripStatusEvent.objects.filter(reservation=cancelled).exists()


def test_clone_takes_its_orders_initial_status():
    booked = TransferReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED))
    quoted = TransferReservationFactory(lead=LeadFactory(status=Lead.Status.QUOTED))
    assert groups.clone_reservation(booked, 5).trip_status == UNASSIGNED
    assert groups.clone_reservation(quoted, 5).trip_status == PENDING


def test_a_return_trip_carries_none_of_the_sources_trip_state(admin_user):
    """Farm-out flag, circling notice and cancel stamps belong to the source trip, not to
    the new one made from it (final fix wave F6)."""
    from django.utils import timezone

    from apps.reservations.routing import create_return_trip

    source = TransferReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        trip_status=Reservation.TripStatus.ARRIVED,
        needs_farm_out=True,
        circling_at=timezone.now(),
        circling_source=Reservation.CirclingSource.MANUAL,
        circling_by=admin_user,
        cancel_reason="Flight grounded",
        cancel_covid=True,
        cancelled_at=timezone.now(),
        cancelled_by=admin_user,
    )

    ret = create_return_trip(source)

    ret.refresh_from_db()
    assert ret.needs_farm_out is False
    assert (ret.circling_at, ret.circling_source, ret.circling_by) == (None, "", None)
    assert (ret.cancel_reason, ret.cancel_covid, ret.cancelled_at, ret.cancelled_by) == (
        "",
        False,
        None,
        None,
    )
