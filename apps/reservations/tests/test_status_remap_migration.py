"""The trip-status cleanup's data migration (spec 2026-11-10 "Data migration"): every
retired value lands on one of the 11 survivors, with the facts it used to carry moved to
their new homes. One test per row of the spec's mapping table."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.audit.factories import AuditEntryFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import RETIRED_STATUS_LABELS, Reservation, TripStatusEvent

pytestmark = pytest.mark.django_db

TS = Reservation.TripStatus
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)

migration = importlib.import_module("apps.reservations.migrations.0035_remap_retired_trip_statuses")


def _remap() -> dict[str, int]:
    return migration.remap(Reservation, Assignment, TripStatusEvent, NOW)


def _trip(raw: str, *, booked: bool = True) -> Reservation:
    """A trip holding a raw stored value, retired or not (choices aren't enforced on save)."""
    status = Lead.Status.BOOKED if booked else Lead.Status.QUOTED
    trip = ReservationFactory(lead=LeadFactory(status=status))
    trip.trip_status = raw
    trip.save(update_fields=["trip_status"])
    return trip


def _covered(trip: Reservation, status: str) -> Assignment:
    return AssignmentFactory(reservation=trip, status=status)


def _status(trip: Reservation) -> str:
    trip.refresh_from_db()
    return trip.trip_status


# --- blank ---


def test_blank_on_an_unbooked_order_becomes_pending():
    trip = _trip("", booked=False)
    assert _remap()[""] == 1
    assert _status(trip) == TS.PENDING


def test_blank_on_a_booked_order_with_no_coverage_becomes_unassigned():
    trip = _trip("")
    _remap()
    assert _status(trip) == TS.UNASSIGNED


def test_blank_on_a_booked_order_with_an_offer_becomes_assigned():
    trip = _trip("")
    _covered(trip, Assignment.Status.OFFERED)
    _remap()
    assert _status(trip) == TS.ASSIGNED


def test_blank_on_a_booked_order_with_confirmed_coverage_becomes_dispatched():
    trip = _trip("")
    _covered(trip, Assignment.Status.CONFIRMED)
    _remap()
    assert _status(trip) == TS.DISPATCHED


def test_blank_ignores_coverage_that_is_no_longer_active():
    trip = _trip("")
    _covered(trip, Assignment.Status.DECLINED)
    _remap()
    assert _status(trip) == TS.UNASSIGNED


# --- retired values ---


def test_farm_out_unassigned_becomes_unassigned_needing_farm_out():
    trip = _trip("farm_out_unassigned")
    assert _remap()["farm_out_unassigned"] == 1
    trip.refresh_from_db()
    assert (trip.trip_status, trip.needs_farm_out) == (TS.UNASSIGNED, True)


@pytest.mark.parametrize("old", ["offered", "offered_to_affiliate"])
def test_offered_values_become_assigned(old):
    trip = _trip(old)
    _covered(trip, Assignment.Status.OFFERED)
    assert _remap()[old] == 1
    assert _status(trip) == TS.ASSIGNED


@pytest.mark.parametrize("old", ["assigned", "affiliate_assigned"])
def test_assigned_values_with_confirmed_coverage_become_dispatched(old):
    trip = _trip(old)
    _covered(trip, Assignment.Status.CONFIRMED)
    assert _remap()[old] == 1
    assert _status(trip) == TS.DISPATCHED


@pytest.mark.parametrize("old", ["assigned", "affiliate_assigned"])
def test_assigned_values_without_confirmed_coverage_become_assigned(old):
    trip = _trip(old)
    _covered(trip, Assignment.Status.OFFERED)
    _remap()
    assert _status(trip) == TS.ASSIGNED


def test_dispatched_non_la_becomes_dispatched():
    trip = _trip("dispatched_non_la")
    assert _remap()["dispatched_non_la"] == 1
    assert _status(trip) == TS.DISPATCHED


def test_circling_becomes_arrived_with_the_notice_from_its_latest_event():
    trip = _trip("circling")
    older = TripStatusEvent.objects.create(reservation=trip, status="circling")
    latest = TripStatusEvent.objects.create(reservation=trip, status="circling")
    at = NOW - timedelta(minutes=7)
    TripStatusEvent.objects.filter(pk=older.pk).update(created_at=at - timedelta(hours=1))
    TripStatusEvent.objects.filter(pk=latest.pk).update(created_at=at)

    assert _remap()["circling"] == 1

    trip.refresh_from_db()
    assert trip.trip_status == TS.ARRIVED
    assert trip.circling_at == at
    assert trip.circling_source == Reservation.CirclingSource.LIMOANYWHERE
    assert trip.circling_by is None


def test_circling_with_no_event_takes_now():
    trip = _trip("circling")
    _remap()
    trip.refresh_from_db()
    assert (trip.trip_status, trip.circling_at) == (TS.ARRIVED, NOW)


def test_cancelled_by_affiliate_becomes_unassigned_needing_farm_out():
    trip = _trip("cancelled_by_affiliate")
    assert _remap()["cancelled_by_affiliate"] == 1
    trip.refresh_from_db()
    assert (trip.trip_status, trip.needs_farm_out) == (TS.UNASSIGNED, True)


def test_covid_cancellation_becomes_cancelled_with_the_covid_flag():
    trip = _trip("covid_cancellation")
    assert _remap()["covid_cancellation"] == 1
    trip.refresh_from_db()
    assert trip.trip_status == TS.CANCELLED
    assert trip.cancel_covid is True
    assert trip.cancel_reason == "COVID-19 (migrated)"


# --- what the migration leaves alone ---


@pytest.mark.parametrize(
    "kept",
    [TS.PENDING, TS.UNASSIGNED, TS.DISPATCHED, TS.ON_THE_WAY, TS.DONE, TS.NO_SHOW],
)
def test_survivors_are_untouched(kept):
    trip = _trip(kept)
    before = Reservation.objects.get(pk=trip.pk).updated_at
    _remap()
    trip.refresh_from_db()
    assert (trip.trip_status, trip.updated_at) == (kept, before)


def test_writes_no_status_events_and_keeps_the_old_ones():
    trip = _trip("covid_cancellation")
    TripStatusEvent.objects.create(reservation=trip, status="covid_cancellation")
    _remap()
    kept = TripStatusEvent.objects.filter(reservation=trip).values_list("status", flat=True)
    assert list(kept) == ["covid_cancellation"]


def test_counts_every_old_value_seen():
    _trip("offered")
    _trip("offered")
    _trip("dispatched_non_la")
    counts = _remap()
    assert counts["offered"] == 2
    assert counts["dispatched_non_la"] == 1
    assert counts["covid_cancellation"] == 0
    assert set(counts) == {"", "assigned", *RETIRED_STATUS_LABELS}


# --- history (Review Focus 5) ---


def test_history_labels_retired_status_values(client):
    event = TripStatusEvent(status="covid_cancellation")
    assert event.status_label == "COVID-19 Cancellation (retired)"
    assert TripStatusEvent(status=TS.DISPATCHED).status_label == "Dispatched"

    trip = ReservationFactory()
    AuditEntryFactory(
        object_id=trip.pk,
        trip_id=trip.pk,
        changes={"trip_status": ["covid_cancellation", "cancelled"]},
        timestamp=timezone.now(),
    )
    client.force_login(UserFactory())
    resp = client.get(reverse("audit:trip", args=[trip.pk]))

    assert resp.status_code == 200
    assert "COVID-19 Cancellation (retired)" in resp.content.decode()
