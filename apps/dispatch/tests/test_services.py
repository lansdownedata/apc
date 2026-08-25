from decimal import Decimal
from unittest.mock import patch

import pytest
import requests

from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.integrations import gnet
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


def _armed(settings):
    """Flip both preview-gating flags off so send_offer/withdraw would actually
    attempt a real HTTP call — needed to exercise gnet.py's transport-failure
    handling rather than short-circuiting in preview mode."""
    settings.GNET_ACTIVE = True
    settings.GNET_API_KEY = "lds_testkey1234567890"


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


def test_release_trips_isolates_a_failure_so_the_rest_of_the_batch_still_releases():
    """A gateway problem can no longer raise out of withdraw() at all (see its
    docstring) — but a genuine bug or a DB hiccup mid-batch still could, and a batch
    that aborts on the first failure would strand every later trip in exactly the
    "affiliate still holds a trip that no longer exists" state this function exists
    to prevent. One bad row must not stop the other rows from releasing."""
    reservations = [_booked_trip() for _ in range(3)]
    assignments = [
        services.send_offer(res, VendorFactory(), payout=Decimal("140.00")) for res in reservations
    ]
    boom = assignments[1]
    real_withdraw = services.withdraw

    def _flaky(assignment, *, note=""):
        if assignment.pk == boom.pk:
            raise RuntimeError("boom")
        return real_withdraw(assignment, note=note)

    with patch.object(services, "withdraw", side_effect=_flaky):
        released = services.release_trips(reservations, note="Order cancelled")

    assert {a.pk for a in released} == {assignments[0].pk, assignments[2].pk}
    for assignment in (assignments[0], assignments[2]):
        assignment.refresh_from_db()
        assert assignment.status == Assignment.Status.WITHDRAWN
    boom.refresh_from_db()
    assert boom.status == Assignment.Status.OFFERED  # never withdrawn — but not fatal


def test_confirming_an_already_confirmed_assignment_is_a_no_op():
    a = AssignmentFactory(status=Assignment.Status.CONFIRMED)
    first_resolved = a.resolved_at
    assert services.confirm(a) is a
    a.refresh_from_db()
    assert a.status == Assignment.Status.CONFIRMED
    assert a.resolved_at == first_resolved


# --- GNet routing ---


def test_send_offer_to_a_gnet_capable_vendor_uses_the_gnet_channel():
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="gnet-partner-1", email="ops@x.example")
    with patch.object(services, "gnet_sync") as mock_sync:
        a = services.send_offer(res, vendor, payout=Decimal("140.00"))
    assert a.channel == Assignment.Channel.GNET
    mock_sync.push_assignment.assert_called_once_with(a)


def test_send_offer_to_a_non_gnet_vendor_uses_the_manual_channel():
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="", email="ops@x.example")
    with patch.object(services, "gnet_sync") as mock_sync:
        a = services.send_offer(res, vendor, payout=Decimal("140.00"))
    assert a.channel == Assignment.Channel.MANUAL
    mock_sync.push_assignment.assert_not_called()


def test_assign_direct_is_always_manual_even_for_a_gnet_capable_vendor():
    """assign_direct records phone-arranged coverage — it never farms out over GNet,
    regardless of whether the vendor carries a griddID."""
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="gnet-partner-1")
    with patch.object(services, "gnet_sync") as mock_sync:
        a = services.assign_direct(res, vendor, payout=Decimal("140.00"))
    assert a.channel == Assignment.Channel.MANUAL
    mock_sync.push_assignment.assert_not_called()


def test_assign_direct_is_manual_for_a_non_gnet_vendor_too():
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="")
    a = services.assign_direct(res, vendor, payout=Decimal("140.00"))
    assert a.channel == Assignment.Channel.MANUAL


def test_withdrawing_a_gnet_assignment_cancels_it_on_the_gateway():
    a = AssignmentFactory(
        status=Assignment.Status.OFFERED,
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
    )
    with patch.object(services, "gnet_sync") as mock_sync:
        services.withdraw(a, note="vendor unreachable")
    mock_sync.cancel_assignment.assert_called_once_with(a)
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN


def test_withdrawing_a_manual_assignment_never_touches_the_gateway():
    a = AssignmentFactory(status=Assignment.Status.OFFERED, channel=Assignment.Channel.MANUAL)
    with patch.object(services, "gnet_sync") as mock_sync:
        services.withdraw(a, note="vendor unreachable")
    mock_sync.cancel_assignment.assert_not_called()


def test_a_transport_failure_during_send_offer_does_not_roll_back_or_raise(settings):
    """A real gateway problem — here, a connection that never reaches api.grdd.net at
    all — is handled entirely inside gnet.py/gnet_sync (converted to a terminal
    GnetEvent + alert, never a raised exception). send_offer must see a normal return,
    not an exception, and the assignment must remain exactly as claimed. This exercises
    the actual `_request` -> `push_assignment` chain, not a stand-in mock, because a
    prior version of this fix let `requests.exceptions.RequestException` escape
    `_request` raw, past `push_assignment`'s `except GnetAPIError`, straight into
    `send_offer` — see apps/integrations/tests/test_gnet_client.py for the
    client-level regression test."""
    _armed(settings)
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="gnet-partner-1")
    with patch.object(gnet, "requests") as req:
        req.request.side_effect = requests.exceptions.ConnectionError("refused")
        a = services.send_offer(res, vendor, payout=Decimal("140.00"))  # must not raise
    req.request.assert_called_once()  # prove this actually reached the transport call
    a.refresh_from_db()
    assert a.status == Assignment.Status.OFFERED
    assert a.channel == Assignment.Channel.GNET


def test_a_transport_failure_during_withdraw_does_not_undo_the_state_change(settings):
    """Same as above but for the cancel side: a Timeout talking to the gateway must
    not undo the WITHDRAWN state change or raise out of withdraw()."""
    _armed(settings)
    a = AssignmentFactory(
        status=Assignment.Status.OFFERED,
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
    )
    with patch.object(gnet, "requests") as req:
        req.request.side_effect = requests.exceptions.Timeout("timed out")
        services.withdraw(a)  # must not raise
    req.request.assert_called_once()  # prove this actually reached the transport call
    a.refresh_from_db()
    assert a.status == Assignment.Status.WITHDRAWN


def test_send_offer_lets_a_genuine_gnet_sync_bug_propagate():
    """Only real gateway problems are best-effort — that guarantee lives inside
    gnet_sync/gnet.py itself (see the transport-failure tests above and in
    test_gnet_sync.py/test_gnet_client.py). send_offer no longer wraps the call in a
    bare `except Exception`, so a genuine bug (a TypeError, say) must be loud rather
    than silently swallowed."""
    res = _booked_trip()
    vendor = VendorFactory(gnet_grid_id="gnet-partner-1")
    with patch.object(services.gnet_sync, "push_assignment", side_effect=TypeError("boom")):
        with pytest.raises(TypeError):
            services.send_offer(res, vendor, payout=Decimal("140.00"))


def test_withdraw_lets_a_genuine_gnet_sync_bug_propagate():
    a = AssignmentFactory(
        status=Assignment.Status.OFFERED,
        channel=Assignment.Channel.GNET,
        gnet_transaction_id="TX-1",
    )
    with patch.object(services.gnet_sync, "cancel_assignment", side_effect=TypeError("boom")):
        with pytest.raises(TypeError):
            services.withdraw(a)
