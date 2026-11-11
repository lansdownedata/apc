"""Trip-status cleanup: every cancellation records a reason, who, when, and COVID-19."""

from unittest.mock import patch

import pytest

from apps.messaging import events
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripStatusEvent
from apps.reservations.services import record_cancellation, set_circling, set_trip_status

pytestmark = pytest.mark.django_db

TS = Reservation.TripStatus


def _triggers(emit):
    return [c.args[0] for c in emit.call_args_list]


@pytest.mark.parametrize("status", [TS.CANCELLED, TS.LATE_CANCEL, TS.NO_SHOW])
def test_it_stamps_the_reason_who_and_when_and_logs_the_status(status, admin_user):
    trip = ReservationFactory(trip_status=TS.DISPATCHED)

    event = record_cancellation(trip, status, reason="  Flight cancelled  ", user=admin_user)

    trip.refresh_from_db()
    assert trip.trip_status == status
    assert trip.cancel_reason == "Flight cancelled"
    assert trip.cancel_covid is False
    assert trip.cancelled_at is not None
    assert trip.cancelled_by == admin_user
    assert isinstance(event, TripStatusEvent)
    assert (event.status, event.changed_by) == (status, admin_user)


def test_the_callers_object_sees_the_cancel_too(admin_user):
    trip = ReservationFactory(trip_status=TS.DISPATCHED)

    record_cancellation(trip, TS.NO_SHOW, reason="Never came down", user=admin_user)

    assert trip.trip_status == TS.NO_SHOW
    assert trip.cancel_reason == "Never came down"
    assert trip.cancelled_by == admin_user and trip.cancelled_at is not None


def test_covid_is_recorded_on_a_plain_cancel():
    trip = ReservationFactory(trip_status=TS.UNASSIGNED)

    record_cancellation(trip, TS.CANCELLED, reason="Lockdown", covid=True)

    trip.refresh_from_db()
    assert trip.trip_status == TS.CANCELLED and trip.cancel_covid is True
    assert trip.cancelled_by is None


def test_a_long_reason_is_cut_to_the_column():
    trip = ReservationFactory(trip_status=TS.UNASSIGNED)
    record_cancellation(trip, TS.CANCELLED, reason="x" * 300)
    trip.refresh_from_db()
    assert trip.cancel_reason == "x" * 255


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_reason_is_required(reason):
    trip = ReservationFactory(trip_status=TS.DISPATCHED)

    with pytest.raises(ValueError, match="Give a reason."):
        record_cancellation(trip, TS.LATE_CANCEL, reason=reason)

    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED and trip.cancelled_at is None
    assert not TripStatusEvent.objects.filter(reservation=trip).exists()


@pytest.mark.parametrize("status", [TS.LATE_CANCEL, TS.NO_SHOW])
def test_covid_is_refused_on_a_chargeable_cancel(status):
    trip = ReservationFactory(trip_status=TS.DISPATCHED)

    with pytest.raises(ValueError):
        record_cancellation(trip, status, reason="Lockdown", covid=True)

    trip.refresh_from_db()
    assert trip.trip_status == TS.DISPATCHED and trip.cancel_covid is False


@pytest.mark.parametrize("status", [TS.DONE, TS.DISPATCHED, "covid_cancellation"])
def test_only_the_three_cancel_statuses_are_accepted(status):
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    with pytest.raises(ValueError):
        record_cancellation(trip, status, reason="Because")
    trip.refresh_from_db()
    assert trip.trip_status == TS.ARRIVED


def test_a_cancelled_trip_cannot_become_a_chargeable_one():
    """A plain cancel has had its money back; a late cancel or no-show would claim it."""
    trip = ReservationFactory(trip_status=TS.CANCELLED)
    with pytest.raises(ValueError, match="already cancelled"):
        record_cancellation(trip, TS.NO_SHOW, reason="Changed my mind")
    trip.refresh_from_db()
    assert trip.trip_status == TS.CANCELLED


def test_a_repeat_of_the_same_cancel_keeps_the_first_reason_who_and_when(admin_user):
    """A second No Show (another dispatcher, another reason) is a no-op: the first stands."""
    from apps.accounts.factories import UserFactory

    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    record_cancellation(trip, TS.NO_SHOW, reason="Never came down", user=admin_user)
    trip.refresh_from_db()
    first_at = trip.cancelled_at

    again = Reservation.objects.get(pk=trip.pk)
    assert record_cancellation(again, TS.NO_SHOW, reason="Wrong door", user=UserFactory()) is None

    trip.refresh_from_db()
    assert trip.cancel_reason == "Never came down"
    assert trip.cancelled_by == admin_user
    assert trip.cancelled_at == first_at
    assert TripStatusEvent.objects.filter(reservation=trip, status=TS.NO_SHOW).count() == 1


def test_it_emits_trip_cancelled_once_by_default():
    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    with patch.object(events, "emit") as emit:
        record_cancellation(trip, TS.LATE_CANCEL, reason="Called at the door")
    assert _triggers(emit).count("trip_cancelled") == 1


def test_emit_false_writes_everything_but_sends_nothing(admin_user):
    trip = ReservationFactory(trip_status=TS.ARRIVED)
    set_circling(trip, on=True, source=Reservation.CirclingSource.MANUAL)
    with patch.object(events, "emit") as emit:
        record_cancellation(trip, TS.CANCELLED, reason="Weather", user=admin_user, emit=False)
    emit.assert_not_called()
    trip.refresh_from_db()
    assert trip.trip_status == TS.CANCELLED and trip.circling_at is None
    assert TripStatusEvent.objects.filter(reservation=trip, status=TS.CANCELLED).count() == 1


# --- set_trip_status: emit flag and the row lock ------------------------------------------


def test_set_trip_status_emit_false_skips_only_the_events():
    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    with patch.object(events, "emit") as emit, patch.object(events, "sync_waiting") as sync:
        event = set_trip_status(trip, TS.ON_THE_WAY, emit=False)
    emit.assert_not_called()
    sync.assert_called_once()
    assert event is not None and event.status == TS.ON_THE_WAY


def test_set_trip_status_compares_against_the_row_not_a_stale_object():
    """Two dispatchers: the second holds an object read before the first one's change."""
    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    stale = Reservation.objects.get(pk=trip.pk)
    set_trip_status(trip, TS.ON_THE_WAY)

    assert set_trip_status(stale, TS.ON_THE_WAY) is None
    assert stale.trip_status == TS.ON_THE_WAY
    assert TripStatusEvent.objects.filter(reservation=trip, status=TS.ON_THE_WAY).count() == 1


def test_set_trip_status_locks_the_row():
    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    real = Reservation.objects.select_for_update
    with patch.object(
        Reservation.objects, "select_for_update", side_effect=lambda *a, **k: real(*a, **k)
    ) as lock:
        set_trip_status(trip, TS.ON_THE_WAY)
    lock.assert_called()


def test_a_failing_farm_in_report_never_undoes_a_status():
    from apps.dispatch import farm_in_push

    trip = ReservationFactory(trip_status=TS.DISPATCHED)
    with patch.object(farm_in_push, "after_trip_status", side_effect=RuntimeError("boom")):
        set_trip_status(trip, TS.ON_THE_WAY)
    trip.refresh_from_db()
    assert trip.trip_status == TS.ON_THE_WAY
