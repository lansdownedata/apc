"""APC-56 — green-lit: a derived per-trip state, never a stored checkbox."""

from datetime import time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch import selectors as dispatch_selectors
from apps.dispatch.board_filters import BoardFilters
from apps.dispatch.models import DispatchException
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation
from apps.tasks import services
from apps.tasks.definitions import KINDS
from apps.tasks.models import Task
from apps.tasks.selectors import attach_green_lit, green_lit, with_green_lit

pytestmark = pytest.mark.django_db

DAY = timezone.localdate() + timedelta(days=5)


def _trip(*, wedding=False, lead=None):
    lead = lead or LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None,
        pickup_date=DAY,
        pickup_time=time(9, 0),
    )
    services.ensure_tasks(lead)
    return trip


def _close_all(trip, status=Task.Status.DONE):
    Task.objects.filter(lead=trip.lead).update(status=status, completed_by=UserFactory())


def test_details_finalized_is_a_manual_operations_trip_task():
    kind = KINDS["details_finalized"]
    trip = _trip()

    assert kind.department == "operations"
    assert kind.level == "trip"
    assert kind.auto_complete is None
    assert Task.objects.filter(reservation=trip, kind="details_finalized").exists()


def test_all_tasks_done_and_no_exception_is_green_lit():
    trip = _trip()
    _close_all(trip)

    assert green_lit(trip) is True


def test_one_open_trip_task_is_not_green_lit():
    trip = _trip()
    _close_all(trip)
    Task.objects.filter(reservation=trip, kind="details_finalized").update(status=Task.Status.OPEN)

    assert green_lit(trip) is False


def test_an_open_dispatch_exception_is_not_green_lit():
    trip = _trip()
    _close_all(trip)
    DispatchException.objects.create(reservation=trip, kind=DispatchException.Kind.NOT_ARRIVED)

    assert green_lit(trip) is False

    DispatchException.objects.filter(reservation=trip).update(resolved_at=timezone.now())
    assert green_lit(trip) is True


def test_a_wedding_order_waits_on_its_open_wedding_tasks():
    trip = _trip(wedding=True)
    _close_all(trip)
    Task.objects.filter(lead=trip.lead, kind="day_of_contact").update(status=Task.Status.OPEN)

    assert green_lit(trip) is False


def test_other_open_order_level_tasks_do_not_block():
    trip = _trip()
    _close_all(trip)
    Task.objects.filter(lead=trip.lead, kind="final_balance_paid").update(status=Task.Status.OPEN)

    assert green_lit(trip) is True


def test_skipped_and_not_applicable_count_as_closed():
    trip = _trip()
    _close_all(trip, status=Task.Status.SKIPPED)
    Task.objects.filter(reservation=trip, kind="driver_released").update(
        status=Task.Status.NOT_APPLICABLE
    )

    assert green_lit(trip) is True


def test_a_trip_with_no_tasks_is_not_green_lit():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=lead, pickup_date=DAY, pickup_time=time(9, 0))

    assert green_lit(trip) is False


def test_the_annotation_agrees_with_the_single_trip_check():
    ready = _trip()
    _close_all(ready)
    waiting = _trip()

    flags = dict(
        with_green_lit(Reservation.objects.filter(pk__in=[ready.pk, waiting.pk])).values_list(
            "pk", "green_lit"
        )
    )

    assert flags == {ready.pk: True, waiting.pk: False}


def test_attach_green_lit_lists_what_is_missing():
    trip = _trip(wedding=True)
    _close_all(trip)
    Task.objects.filter(reservation=trip, kind="details_finalized").update(status=Task.Status.OPEN)
    Task.objects.filter(lead=trip.lead, kind="wedding_names").update(status=Task.Status.OPEN)
    DispatchException.objects.create(reservation=trip, kind=DispatchException.Kind.UNASSIGNED)

    [decorated] = attach_green_lit([trip])

    assert decorated.green_lit is False
    assert decorated.task_blockers == [
        "Details finalized",
        "Wedding names collected",
        "No coverage",
    ]


def _filters():
    return BoardFilters(view="range", start=DAY, end=DAY, anchor=DAY)


def test_board_trips_carry_green_lit_and_the_query_count_is_flat():
    for _ in range(5):
        _trip()
    with CaptureQueriesContext(connection) as few:
        trips = dispatch_selectors.board_trips(_filters())
    assert all(hasattr(t, "green_lit") for t in trips)

    for _ in range(45):
        _trip()
    with CaptureQueriesContext(connection) as many:
        trips = dispatch_selectors.board_trips(_filters())

    assert len(trips) == 50
    assert len(many) == len(few)


def test_the_board_and_drawer_show_the_pill(client):
    client.force_login(UserFactory())
    ready = _trip()
    _close_all(ready)
    waiting = _trip()

    board = client.get(reverse("dispatch_board"), {"day": DAY.isoformat()}).content.decode()
    assert "Green-lit" in board
    assert "open" in board

    drawer = client.get(reverse("dispatch_assign_panel", args=[waiting.pk])).content.decode()
    assert "open</span>" in drawer
    assert "Details finalized" in drawer
