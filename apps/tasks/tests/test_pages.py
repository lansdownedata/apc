"""APC-53 page + APC-54 checklists — the templates on top of the queue / action backend.

Agreed layout (2026-09-26): the queue page as mocked up; trip-level checks live in the
dispatch drawer and in a per-trip checklist opened from the trip line's icon; the order
page and workspace carry the order-level checklist only.
"""

import re
from datetime import time, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import ReservationFactory
from apps.tasks import services
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture
def me(client):
    user = UserFactory(first_name="Moe")
    client.force_login(user)
    return user


def _order(trips=1, *, wedding=False, status=Lead.Status.BOOKED):
    lead = LeadFactory(status=status)
    service = ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None
    for i in range(trips):
        ReservationFactory(
            lead=lead,
            service_type=service,
            pickup_date=timezone.localdate() + timedelta(days=10 + i),
            pickup_time=time(9, 0),
            pickup_timezone="America/New_York",
        )
    services.ensure_tasks(lead)
    return lead


def _mine(lead, user, kind, *, hours=-2):
    task = Task.objects.get(lead=lead, kind=kind, reservation__isnull=kind != "details_finalized")
    Task.objects.filter(pk=task.pk).update(
        assignee=user, due_at=timezone.now() + timedelta(hours=hours)
    )
    return Task.objects.get(pk=task.pk)


# --- the queue page ----------------------------------------------------------------


def test_the_queue_page_lists_my_tasks_overdue_first(client, me):
    lead = _order()
    later = _mine(lead, me, "details_finalized", hours=30)
    late = _mine(lead, me, "final_itinerary", hours=-3)

    resp = client.get(reverse("task_queue"))
    body = resp.content.decode()

    assert resp.status_code == 200
    assert [t.pk for t in resp.context["rows"]] == [late.pk, later.pk]
    assert body.index("Final itinerary received") < body.index("Details finalized")
    for header in ("Task", "Order / trip", "Due", "Assignee"):
        assert f">{header}</th>" in body
    assert "sticky" in body
    assert "1 overdue" in body


def test_the_queue_filters_are_tom_selects_that_autosubmit(client, me):
    body = client.get(reverse("task_queue")).content.decode()

    selects = re.findall(r"<select[^>]*>", body)
    names = {re.search(r'name="(\w+)"', s).group(1) for s in selects}
    assert {"department", "assignee", "due", "kind"} <= names
    for tag in selects:
        assert "data-tom" in tag


def test_the_queue_filters_narrow_the_rows(client, me):
    lead = _order()
    _mine(lead, me, "details_finalized")
    itinerary = _mine(lead, me, "final_itinerary")

    resp = client.get(reverse("task_queue"), {"kind": "final_itinerary"})

    assert [t.pk for t in resp.context["rows"]] == [itinerary.pk]


def test_trip_rows_open_the_drawer_and_order_rows_open_the_workspace(client, me):
    lead = _order()
    trip = lead.reservations.get()
    _mine(lead, me, "details_finalized")
    _mine(lead, me, "final_itinerary")

    body = client.get(reverse("task_queue")).content.decode()

    board = reverse("dispatch_board")
    assert f"{board}?day={trip.pickup_date.isoformat()}&amp;trip={trip.pk}" in body
    assert reverse("lead_detail", args=[lead.pk]) in body


def test_the_empty_state(client, me):
    body = client.get(reverse("task_queue")).content.decode()

    assert "Nothing due" in body


def test_the_page_query_count_is_flat(client, me, django_assert_max_num_queries):
    for _ in range(3):
        _mine(_order(), me, "details_finalized")
    with CaptureQueriesContext(connection) as few:
        client.get(reverse("task_queue"))
    for _ in range(20):
        _mine(_order(), me, "details_finalized")
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("task_queue"))

    assert len(many) == len(few)


def test_the_nav_has_tasks_with_my_badge(client, me):
    lead = _order()
    _mine(lead, me, "final_itinerary", hours=-2)
    _mine(lead, me, "details_finalized", hours=72)

    body = client.get(reverse("dashboard")).content.decode()

    assert reverse("task_queue") in body
    assert 'data-task-badge="1"' in body


def test_board_rows_can_be_deep_linked(client, me):
    lead = _order()
    trip = lead.reservations.get()

    body = client.get(
        reverse("dispatch_board"), {"day": trip.pickup_date.isoformat(), "trip": trip.pk}
    ).content.decode()

    assert f'data-trip="{trip.pk}"' in body


# --- checklists (APC-54) -----------------------------------------------------------


def test_the_trip_checklist_shows_only_that_trip_plus_the_wedding_blockers(client, me):
    lead = _order(trips=2, wedding=True)
    first, second = lead.reservations.order_by("pickup_date")
    Task.objects.filter(reservation=second, kind="details_finalized").update(note="SECOND-TRIP")

    resp = client.get(reverse("trip_checklist", args=[first.pk]))
    body = resp.content.decode()

    assert resp.status_code == 200
    shown = {t.pk for t in resp.context["tasks"]}
    assert shown == set(Task.objects.filter(reservation=first).values_list("pk", flat=True)) | set(
        Task.objects.filter(lead=lead, kind__in=("wedding_names", "day_of_contact")).values_list(
            "pk", flat=True
        )
    )
    assert "Details finalized" in body
    assert "Wedding names collected" in body
    assert "Final balance paid" not in body
    assert "Complete" in body


def test_the_order_checklist_is_order_level_only(client, me):
    lead = _order()

    resp = client.get(reverse("order_checklist", args=[lead.pk]))

    assert resp.status_code == 200
    assert all(t.reservation_id is None for t in resp.context["tasks"])
    assert "Final itinerary received" in resp.content.decode()
    assert "Details finalized" not in resp.content.decode()


def test_done_items_say_who_or_auto(client, me):
    lead = _order()
    trip = lead.reservations.get()
    services.complete(Task.objects.get(reservation=trip, kind="details_finalized"), user=me)
    Task.objects.filter(reservation=trip, kind="affiliate_assigned").update(
        status=Task.Status.DONE, completed_at=timezone.now(), completed_by=None
    )

    body = client.get(reverse("trip_checklist", args=[trip.pk])).content.decode()

    assert "Moe" in body
    assert "auto" in body


def test_the_workspace_and_order_page_carry_the_order_checklist_and_trip_icons(client, me):
    lead = _order(trips=2)

    for url in (reverse("lead_detail", args=[lead.pk]), reverse("order_detail", args=[lead.pk])):
        resp = client.get(url)
        body = resp.content.decode()
        assert reverse("order_checklist", args=[lead.pk]) in body, url
        for trip in lead.reservations.all():
            assert reverse("trip_checklist", args=[trip.pk]) in body, url
        # trip-level checks stay behind the icon, not in the order card
        assert resp.context["order_tasks"]
        assert all(t.reservation_id is None for t in resp.context["order_tasks"])


def test_an_unbooked_quote_shows_no_tasks_yet(client, me):
    lead = _order(status=Lead.Status.QUOTED)

    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert "No tasks yet" in body
    assert reverse("trip_checklist", args=[lead.reservations.get().pk]) not in body


def test_the_drawer_embeds_the_trip_checklist_with_flat_queries(client, me):
    lead = _order()
    trip = lead.reservations.get()
    url = reverse("dispatch_assign_panel", args=[trip.pk])

    with CaptureQueriesContext(connection) as before:
        body = client.get(url).content.decode()
    for i in range(10):
        Task.objects.create(
            lead=lead,
            reservation=trip,
            kind=f"extra_{i}",
            department="operations",
            opens_at=timezone.now(),
        )
    with CaptureQueriesContext(connection) as after:
        client.get(url)

    assert "Details finalized" in body
    assert reverse("trip_checklist", args=[trip.pk]) in body
    assert len(after) == len(before)


def test_completing_from_the_drawer_updates_the_task_and_the_queue(client, me):
    lead = _order()
    task = _mine(lead, me, "details_finalized")

    client.post(reverse("task_complete", args=[task.pk]))

    task.refresh_from_db()
    assert task.status == Task.Status.DONE
    queue = client.get(reverse("task_queue"))
    assert task.pk not in {t.pk for t in queue.context["rows"]}
