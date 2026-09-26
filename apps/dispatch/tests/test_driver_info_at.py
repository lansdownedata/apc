"""APC-57 — `Assignment.driver_info_at`: when driver info was first complete.

Phase D scores affiliates on how early driver info arrives, so the first stamp is history
that must never be overwritten by a later edit.
"""

from datetime import date, time, timedelta

import pytest
from django.utils import timezone

from apps.dispatch import services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.fleet.factories import DriverFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


def _trip():
    return ReservationFactory(
        lead=LeadFactory(status=Lead.Status.BOOKED),
        pickup_date=date(2026, 8, 26),
        pickup_time=time(6, 15),
    )


def _set(a, **overrides):
    fields = {
        "name": "Sam Rivera",
        "cell": "+15715551212",
        "vehicle_desc": "",
        "vehicle_number": "",
    }
    fields.update(overrides)
    return services.set_driver_info(a, **fields)


def test_first_complete_set_driver_info_stamps_and_a_later_edit_keeps_it():
    a = AssignmentFactory(reservation=_trip(), status=Assignment.Status.CONFIRMED)

    _set(a)
    a.refresh_from_db()
    first = a.driver_info_at
    assert first is not None

    Assignment.objects.filter(pk=a.pk).update(driver_info_at=first - timedelta(hours=3))
    a.refresh_from_db()
    _set(a, vehicle_number="12")

    a.refresh_from_db()
    assert a.driver_info_at == first - timedelta(hours=3)


def test_a_partial_update_without_a_cell_does_not_stamp():
    a = AssignmentFactory(reservation=_trip(), status=Assignment.Status.CONFIRMED)

    _set(a, cell="")

    a.refresh_from_db()
    assert a.driver_info_at is None


def test_completing_a_partial_record_later_stamps_then():
    a = AssignmentFactory(reservation=_trip(), status=Assignment.Status.CONFIRMED)
    _set(a, cell="")

    _set(a)

    a.refresh_from_db()
    assert a.driver_info_at is not None


def test_in_house_coverage_stamps_on_assignment():
    before = timezone.now()

    a = services.assign_in_house(_trip(), DriverFactory())

    a.refresh_from_db()
    assert a.driver_info_at is not None
    assert a.driver_info_at >= before.replace(microsecond=0)


def test_a_farm_out_confirmation_does_not_stamp():
    a = AssignmentFactory(reservation=_trip(), status=Assignment.Status.OFFERED)

    services.confirm(a)

    a.refresh_from_db()
    assert a.driver_info_at is None
