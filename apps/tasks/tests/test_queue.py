"""APC-53 — the task queue's selector, filters, row actions and nav badge.

The page template waits on the mockup sign-off; everything under it is built and tested
here so the template is the only thing left.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import Department
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import queue
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db


def _lead(tz="America/New_York", days=10):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=days),
        pickup_time=time(9, 0),
        pickup_timezone=tz,
    )
    return lead, trip


def _task(*, lead=None, trip=None, kind="final_itinerary", due=None, assignee=None, **kw):
    if lead is None:
        lead, trip = _lead()
    kw.setdefault("department", Department.CUSTOMER_SERVICE)
    kw.setdefault("status", Task.Status.OPEN)
    return Task.objects.create(
        lead=lead,
        reservation=trip if kind in queue.TRIP_KIND_KEYS else None,
        kind=kind,
        opens_at=timezone.now() - timedelta(days=3),
        due_at=due,
        assignee=assignee,
        **kw,
    )


def _now():
    return timezone.now()


# --- selector: ordering + default scope ----------------------------------------------


def test_default_is_my_open_tasks_overdue_first_then_by_due():
    me = UserFactory()
    later = _task(assignee=me, due=_now() + timedelta(days=3))
    overdue = _task(assignee=me, due=_now() - timedelta(hours=5))
    soon = _task(assignee=me, due=_now() + timedelta(hours=2))
    no_due = _task(assignee=me, due=None)
    _task(assignee=UserFactory(), due=_now() - timedelta(days=1))  # someone else's
    _task(assignee=me, due=_now() - timedelta(days=9), status=Task.Status.DONE)
    _task(assignee=me, due=_now() + timedelta(days=1), status=Task.Status.SCHEDULED)

    rows = list(queue.queue_for(me, queue.QueueFilters()))

    assert rows == [overdue, soon, later, no_due]
    assert rows[0].is_overdue is True
    assert rows[1].is_overdue is False


def test_department_filter():
    me = UserFactory()
    cs = _task(assignee=me, due=_now())
    acct = _task(assignee=me, due=_now(), department=Department.ACCOUNTING)

    rows = queue.queue_for(me, queue.QueueFilters(department=Department.ACCOUNTING))

    assert list(rows) == [acct]
    assert cs not in rows


def test_assignee_filters_unassigned_anyone_and_a_named_user():
    me, other = UserFactory(), UserFactory()
    mine = _task(assignee=me, due=_now())
    theirs = _task(assignee=other, due=_now())
    nobody = _task(assignee=None, due=_now())

    assert list(queue.queue_for(me, queue.QueueFilters(assignee="unassigned"))) == [nobody]
    assert set(queue.queue_for(me, queue.QueueFilters(assignee="anyone"))) == {
        mine,
        theirs,
        nobody,
    }
    assert list(queue.queue_for(me, queue.QueueFilters(assignee=str(other.pk)))) == [theirs]


def test_due_window_filters():
    me = UserFactory()
    overdue = _task(assignee=me, due=_now() - timedelta(hours=1))
    today = _task(assignee=me, due=timezone.localtime().replace(hour=23, minute=0))
    week = _task(assignee=me, due=_now() + timedelta(days=5))
    far = _task(assignee=me, due=_now() + timedelta(days=30))

    assert list(queue.queue_for(me, queue.QueueFilters(due="overdue"))) == [overdue]
    assert today in queue.queue_for(me, queue.QueueFilters(due="today"))
    assert far not in queue.queue_for(me, queue.QueueFilters(due="today"))
    in_week = set(queue.queue_for(me, queue.QueueFilters(due="week")))
    assert week in in_week and far not in in_week


def test_kind_filter():
    me = UserFactory()
    lead, trip = _lead()
    itinerary = _task(lead=lead, trip=trip, assignee=me, due=_now())
    finalize = _task(lead=lead, trip=trip, kind="details_finalized", assignee=me, due=_now())

    assert list(queue.queue_for(me, queue.QueueFilters(kind="details_finalized"))) == [finalize]
    assert itinerary not in queue.queue_for(me, queue.QueueFilters(kind="details_finalized"))


def test_filters_parse_from_the_query_string():
    f = queue.QueueFilters.from_query(
        {"department": "accounting", "assignee": "anyone", "due": "week", "kind": "nope"}
    )

    assert f.department == "accounting"
    assert f.assignee == "anyone"
    assert f.due == "week"
    assert f.kind == ""  # unknown kinds are dropped, not trusted
    assert queue.QueueFilters.from_query({}).assignee == "me"


def test_due_renders_in_the_trip_timezone_not_the_viewers():
    me = UserFactory()
    lead, trip = _lead(tz="America/Los_Angeles")
    due = datetime(2026, 10, 1, 14, 30, tzinfo=ZoneInfo("UTC"))  # 7:30 AM PDT
    _task(lead=lead, trip=trip, kind="details_finalized", assignee=me, due=due)
    _task(lead=lead, trip=trip, assignee=me, due=due)  # order-level: the order's first trip

    rows = list(queue.queue_for(me, queue.QueueFilters()))

    assert {r.due_display for r in rows} == {"Oct 1, 7:30 AM PDT"}


def _rows(user):
    return [
        (r.label, r.department_label, r.lead.quote_no, r.due_display, r.pickup_display, r.assignee)
        for r in queue.queue_for(user, queue.QueueFilters())
    ]


def _trip_task(user):
    lead, trip = _lead()
    _task(lead=lead, trip=trip, kind="details_finalized", assignee=user, due=_now())


def test_query_count_is_one_whether_five_rows_or_a_hundred():
    me = UserFactory()
    for _ in range(5):
        _trip_task(me)
    with CaptureQueriesContext(connection) as few:
        _rows(me)
    for _ in range(95):
        _trip_task(me)
    with CaptureQueriesContext(connection) as many:
        rows = _rows(me)

    assert len(rows) == 100
    assert len(many) == len(few) == 1


# --- badge -------------------------------------------------------------------------


def test_badge_counts_my_overdue_and_due_today_in_one_query():
    me = UserFactory()
    _task(assignee=me, due=_now() - timedelta(days=2))
    _task(assignee=me, due=timezone.localtime().replace(hour=23, minute=30))
    _task(assignee=me, due=_now() + timedelta(days=3))
    _task(assignee=UserFactory(), due=_now() - timedelta(days=1))
    _task(assignee=me, due=_now() - timedelta(days=1), status=Task.Status.DONE)

    with CaptureQueriesContext(connection) as ctx:
        count = queue.badge_count(me)

    assert count == 2
    assert len(ctx) == 1


# --- row actions -------------------------------------------------------------------


@pytest.fixture
def staff(client):
    user = UserFactory()
    client.force_login(user)
    return user


def test_complete_sets_the_fields_and_returns_the_row(client, staff):
    task = _task(assignee=staff, due=_now())

    resp = client.post(reverse("task_complete", args=[task.pk]), {"note": "Got it by email"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["task"]["status"] == "done"
    task.refresh_from_db()
    assert task.status == Task.Status.DONE
    assert task.completed_by == staff
    assert task.note == "Got it by email"


def test_skip_without_a_note_is_refused(client, staff):
    task = _task(assignee=staff, due=_now())

    resp = client.post(reverse("task_skip", args=[task.pk]), {"note": " "})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    task.refresh_from_db()
    assert task.status == Task.Status.OPEN


def test_skip_with_a_note(client, staff):
    task = _task(assignee=staff, due=_now())

    resp = client.post(reverse("task_skip", args=[task.pk]), {"note": "Customer has none"})

    assert resp.status_code == 200
    task.refresh_from_db()
    assert task.status == Task.Status.SKIPPED
    assert task.completed_by == staff


def test_reassign_to_someone_and_to_nobody(client, staff):
    other = UserFactory(first_name="Dana")
    task = _task(assignee=staff, due=_now())

    resp = client.post(reverse("task_reassign", args=[task.pk]), {"assignee": other.pk})
    assert resp.status_code == 200
    assert resp.json()["task"]["assignee"] == "Dana"
    task.refresh_from_db()
    assert task.assignee == other

    client.post(reverse("task_reassign", args=[task.pk]), {"assignee": ""})
    task.refresh_from_db()
    assert task.assignee is None


def test_reassign_to_an_unknown_user_is_refused(client, staff):
    task = _task(assignee=staff, due=_now())

    resp = client.post(reverse("task_reassign", args=[task.pk]), {"assignee": "999999"})

    assert resp.status_code == 400


def test_reopen(client, staff):
    task = _task(assignee=staff, due=_now(), status=Task.Status.DONE, completed_by=staff)

    resp = client.post(reverse("task_reopen", args=[task.pk]))

    assert resp.status_code == 200
    task.refresh_from_db()
    assert task.status == Task.Status.OPEN


def test_actions_need_a_login_and_a_post(client):
    task = _task(due=_now())

    assert client.post(reverse("task_complete", args=[task.pk])).status_code == 302
    client.force_login(UserFactory())
    assert client.get(reverse("task_complete", args=[task.pk])).status_code == 405
