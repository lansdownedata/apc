"""Sent back: the vendor gave a confirmed trip back (trip-status cleanup, Task 4)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import gnet_callback, services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.notifications.models import Notification
from apps.payments import payables
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors import performance
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db
TS = Reservation.TripStatus


def _trip(**kwargs):
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    kwargs.setdefault("trip_status", TS.DISPATCHED)
    return ReservationFactory(**kwargs)


def _confirmed(trip=None, **kwargs):
    kwargs.setdefault("vendor", VendorFactory())
    kwargs.setdefault("status", Assignment.Status.CONFIRMED)
    return AssignmentFactory(reservation=trip or _trip(), **kwargs)


def test_send_back_reopens_and_flags_farm_out():
    assignment = _confirmed()
    user = UserFactory()

    result = services.send_back(assignment, reason="Driver fell through", user=user)

    result.refresh_from_db()
    assert result.status == Assignment.Status.SENT_BACK
    assert result.sent_back_reason == "Driver fell through"
    assert result.sent_back_by == user
    assert result.resolved_at is not None
    trip = result.reservation
    trip.refresh_from_db()
    assert trip.trip_status == TS.UNASSIGNED
    assert trip.needs_farm_out is True
    assert not trip.assignments.active().exists()


def test_send_back_withdraws_the_t48_affiliate_message():
    assignment = _confirmed()
    trip = assignment.reservation
    with patch("apps.messaging.events.cancel") as cancel:
        services.send_back(assignment, reason="Gave it back")
    cancel.assert_called_once()
    assert cancel.call_args.kwargs["reservation"] == trip


def test_send_back_refuses_in_house_offered_and_blank_reason():
    in_house = AssignmentFactory(reservation=_trip(), in_house=True)
    offered = _confirmed(status=Assignment.Status.OFFERED)
    confirmed = _confirmed()
    with pytest.raises(services.AssignmentError):
        services.send_back(in_house, reason="x")
    with pytest.raises(services.AssignmentError):
        services.send_back(offered, reason="x")
    with pytest.raises(services.AssignmentError):
        services.send_back(confirmed, reason="   ")
    confirmed.refresh_from_db()
    assert confirmed.status == Assignment.Status.CONFIRMED


def _gnet_confirmed(**kwargs):
    return _confirmed(
        vendor=VendorFactory(gnet_grid_id="gnet-partner-1"),
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
        payout=Decimal("140.00"),
        **kwargs,
    )


def test_gnet_cancel_on_confirmed_assignment_is_sent_back():
    assignment = _gnet_confirmed()
    with patch.object(services.gnet_sync, "cancel_assignment") as cancel:
        gnet_callback._apply_status(assignment, "CANCEL", {})
    assert not cancel.called
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.SENT_BACK
    assert assignment.sent_back_reason == "Cancelled on GNet"
    assert assignment.sent_back_by is None
    assert assignment.reservation.needs_farm_out is True
    assert Notification.objects.filter(
        lead=assignment.reservation.lead, kind=Notification.Kind.SYNC_FAILED
    ).exists()


def test_gnet_cancel_on_offered_assignment_still_withdraws():
    assignment = _gnet_confirmed()
    Assignment.objects.filter(pk=assignment.pk).update(status=Assignment.Status.OFFERED)
    assignment.refresh_from_db()
    gnet_callback._apply_status(assignment, "CANCEL", {})
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.WITHDRAWN


def test_scorecard_counts_sent_back_assignments():
    vendor = VendorFactory()
    today = timezone.localdate()
    for _ in range(3):
        _confirmed(_trip(pickup_date=today - timedelta(days=5)), vendor=vendor)
    gave_back = _confirmed(_trip(pickup_date=today - timedelta(days=5)), vendor=vendor)
    services.send_back(gave_back, reason="No driver")

    card = next(
        c
        for c in performance.scorecards(today - timedelta(days=30), today, min_trips=1)
        if c.vendor == vendor
    )

    assert card.cancellations == 1
    assert card.trips == 3


def test_sent_back_assignment_makes_no_payable():
    assignment = _confirmed()
    trip = assignment.reservation
    services.send_back(assignment, reason="No driver")
    assert payables.ensure_payable(trip) is None


def test_send_back_view(client):
    client.force_login(UserFactory())
    assignment = _confirmed()
    url = reverse("dispatch_send_back", args=[assignment.pk])

    blank = client.post(url, {"reason": " "})
    assert blank.status_code == 400
    assert blank.json()["ok"] is False

    ok = client.post(url, {"reason": "Vendor cancelled"})
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.SENT_BACK
    assert assignment.sent_back_by is not None


# --- refusals (final fix wave F3) ----------------------------------------------------------


@pytest.mark.parametrize("status", [TS.DONE, TS.NO_SHOW])
def test_send_back_refuses_a_done_or_cancelled_trip(status):
    assignment = _confirmed(_trip(trip_status=status))

    with pytest.raises(services.AssignmentError):
        services.send_back(assignment, reason="Gave it back")

    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED
    trip = assignment.reservation
    trip.refresh_from_db()
    assert trip.trip_status == status
    assert trip.needs_farm_out is False


def test_send_back_reopens_inside_its_own_transaction():
    """The release and reopen run under the trip lock, so a re-claim can't slip into a gap."""
    from django.db import connection

    assignment = _confirmed()
    outside = len(connection.atomic_blocks)
    depths = []

    def record(*args, **kwargs):
        depths.append(len(connection.atomic_blocks))

    with (
        patch.object(services, "_reopen", side_effect=record),
        patch.object(services, "_on_assignment_released", side_effect=record),
    ):
        services.send_back(assignment, reason="Gave it back")

    assert len(depths) == 2
    assert all(depth > outside for depth in depths)


def test_send_back_view_refuses_a_gnet_assignment(client):
    client.force_login(UserFactory())
    assignment = _gnet_confirmed()

    resp = client.post(reverse("dispatch_send_back", args=[assignment.pk]), {"reason": "x"})

    assert resp.status_code == 400
    assert resp.json()["error"] == "GNet trips are sent back on GNet."
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.CONFIRMED


def test_send_back_view_404s_on_a_missing_assignment(client):
    client.force_login(UserFactory())
    resp = client.post(reverse("dispatch_send_back", args=[999999]), {"reason": "x"})
    assert resp.status_code == 404
