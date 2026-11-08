"""Reservation entries: parent columns, a cheap label, and exclusions."""

from datetime import time
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from apps.audit import registry
from apps.audit.models import AuditEntry
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripReview

pytestmark = pytest.mark.django_db


def _entries(reservation: Reservation, action: int):
    """The trip's own entries: by content type too, since pks repeat across tables."""
    ct = ContentType.objects.get_for_model(Reservation)
    return AuditEntry.objects.filter(content_type=ct, object_id=reservation.pk, action=action)


def _updates(reservation: Reservation):
    return _entries(reservation, AuditEntry.Action.UPDATE)


def test_reservation_update_logs_one_entry_with_parents():
    r = ReservationFactory()
    r.pickup_time = time(7, 30)
    r.save()

    [entry] = _updates(r)
    assert entry.trip_id == r.pk
    assert entry.order_id == r.lead_id
    assert entry.object_repr == r.reference
    assert "pickup_time" in entry.changes


def test_create_entry_holds_only_the_trips_own_columns():
    r = ReservationFactory()

    entry = _entries(r, AuditEntry.Action.CREATE).get()
    assert "pickup_timezone" in entry.changes
    assert not {"stops", "charges", "review", "farm_in"} & set(entry.changes)


def test_create_leaves_reverse_one_to_ones_unread():
    """Reading `review` on a new trip caches "none" on the instance, hiding a review
    that is created later (the trip-processing flow)."""
    r = ReservationFactory()
    TripReview.objects.create(reservation_id=r.pk)

    assert r.review.reservation_id == r.pk


def test_excluded_only_change_writes_nothing():
    r = ReservationFactory()
    r.extras_total = 99
    r.sort_order = 5
    r.save()

    assert not _updates(r).exists()


def test_updated_at_only_change_writes_nothing():
    r = ReservationFactory()
    r.updated_at = timezone.now()
    r.save(update_fields=["updated_at"])
    r.save()

    assert not _updates(r).exists()


def test_update_fields_save_diffs_only_listed_fields():
    r = ReservationFactory()
    r.pickup_time = time(9, 15)
    r.passengers = 7
    r.save(update_fields=["pickup_time"])

    [entry] = _updates(r)
    assert set(entry.changes) == {"pickup_time"}


def test_same_number_in_another_form_is_not_a_change():
    """The editor assigns Decimal("185") over a stored 185.00; auditlog compares strings."""
    r = ReservationFactory()
    r.refresh_from_db()
    r.rate = Decimal("185")
    r.hours = 1  # an int over the stored 1.00: another form of the same number
    r.save()

    assert not _updates(r).exists()


def test_a_real_number_change_still_logs_only_that_field():
    r = ReservationFactory()
    r.rate = Decimal("190")
    r.save()

    [entry] = _updates(r)
    assert entry.changes == {"rate": ["185.00", "190"]}


def test_effective_exclude_adds_the_timestamps_to_the_spec():
    exclude = registry.effective_exclude(Reservation)

    assert {"created_at", "updated_at", "extras_total", "sort_order"} <= set(exclude)
