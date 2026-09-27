"""APC-59 — the Trip Review list and order figures (signed-off design, 2026-09-26)."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import ledger
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import JournalEntry
from apps.reservations import review_queue, reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.tasks import services as tasks
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(trips=1, *, days_ago=1, rate="200", hours="2", total=None, collected="0"):
    """A booked order whose trips ran `days_ago`, entered into review."""
    lead = LeadFactory(status=Lead.Status.BOOKED)
    for i in range(trips):
        trip = ReservationFactory(
            lead=lead,
            pickup_date=timezone.localdate() - timedelta(days=days_ago),
            pickup_time=time(9 + i, 0),
            pickup_timezone="America/New_York",
            rate=Decimal(rate),
            hours=Decimal(hours),
            trip_status=Reservation.TripStatus.DONE,
        )
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    PaymentPlanFactory(lead=lead, quote_total=Decimal(total or lead.quote_total))
    if Decimal(collected):
        ledger.post_capture(
            lead=lead,
            amount=Decimal(collected),
            kind=JournalEntry.Kind.DEPOSIT_CAPTURED,
            idempotency_key=f"test-{lead.pk}",
        )
    run_tasks()
    return lead


def _review(trip, *, over=0, billable=0, waived=False, complete=True):
    start = trip.pickup_at
    end = start + timedelta(hours=float(trip.billed_hours), minutes=over)
    review = reviews.save_review(
        reviews.review_for(trip),
        user=UserFactory(),
        actual_pickup_at=start,
        actual_dropoff_at=end,
        billable_overtime_minutes=billable,
        overtime_waived=waived,
        waive_reason="Traffic" if waived else "",
    )
    if complete:
        reviews.complete_review(review, user=UserFactory())
    return review


def _row(lead, **filters):
    rows = review_queue.orders_in_review(review_queue.Filters(**filters))
    return next((r for r in rows if r.lead.pk == lead.pk), None)


# --- who's listed --------------------------------------------------------------------


def test_an_order_in_review_is_listed_with_its_money():
    lead = _order(trips=2, collected="400")  # 2 × ($200 × 2h) = $800

    row = _row(lead)

    assert row is not None
    assert (row.reviewed, row.to_review) == (0, 2)
    assert row.stage == review_queue.Stage.NEEDS_REVIEW
    assert row.collected == Decimal("400.00")
    assert row.order_total == Decimal("800.00")
    assert row.total_due == Decimal("800.00")
    assert row.remaining == Decimal("400.00")


def test_an_order_still_running_isnt_listed():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=3),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    run_tasks()

    assert _row(lead) is None


def test_approved_overtime_adds_to_total_due_and_waived_doesnt():
    lead = _order(trips=2, collected="800")
    first, second = lead.reservations.order_by("pk")
    _review(first, over=30, billable=30)  # $200/h → $100
    _review(second, over=30, billable=30, waived=True)

    row = _row(lead)

    assert row.approved_overtime == Decimal("100.00")
    assert row.approved_trips == 1
    assert row.total_due == Decimal("900.00")
    assert row.remaining == Decimal("100.00")
    assert row.stage == review_queue.Stage.BILLING


def test_overtime_on_a_review_not_yet_completed_is_awaiting_not_approved():
    lead = _order(trips=1, collected="400")
    _review(lead.reservations.get(), over=30, billable=30, complete=False)

    row = _row(lead)

    assert row.approved_overtime == Decimal("0.00")
    assert row.awaiting_trips == 1


def test_an_order_moves_to_billing_once_every_trip_is_reviewed():
    lead = _order(trips=2, collected="0")
    first, second = lead.reservations.order_by("pk")
    _review(first)
    assert _row(lead).stage == review_queue.Stage.NEEDS_REVIEW

    _review(second)

    row = _row(lead)
    assert (row.reviewed, row.to_review) == (2, 0)
    assert row.stage == review_queue.Stage.BILLING


def test_a_reviewed_and_paid_order_is_closed_and_leaves_the_default_list():
    lead = _order(trips=1, collected="400")
    trip = lead.reservations.get()
    _review(trip)
    # Closing a stage opens the next, so keep going until nothing is left.
    while Task.objects.filter(lead=lead, status__in=Task.UNRESOLVED).exists():
        for task in Task.objects.filter(lead=lead, status__in=Task.UNRESOLVED):
            tasks.complete(task, UserFactory())

    assert _row(lead) is None
    closed = _row(lead, stage="closed")
    assert closed.stage == review_queue.Stage.CLOSED


def test_open_accounting_work_keeps_a_paid_order_in_billing():
    lead = _order(trips=1, collected="400")
    _review(lead.reservations.get())  # farmed out: the payable tasks are still open

    assert _row(lead).stage == review_queue.Stage.BILLING


def test_a_trip_closed_by_hand_on_the_checklist_counts_as_reviewed():
    lead = _order(trips=1)
    tasks.complete(Task.objects.get(lead=lead, kind="ops_review"), UserFactory())

    assert _row(lead).reviewed == 1


def test_cancelled_trips_need_no_review():
    lead = _order(trips=1)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() - timedelta(days=1),
        pickup_time=time(14, 0),
        pickup_timezone="America/New_York",
        trip_status=Reservation.TripStatus.CANCELLED,
    )

    row = _row(lead)

    assert (row.reviewed, row.to_review) == (0, 1)
    assert [t.state for t in row.trips] == ["needs_review", "cancelled"]


def test_open_issues_are_counted():
    lead = _order(trips=1)
    review = reviews.review_for(lead.reservations.get())
    reviews.add_issue(
        review,
        category=TripIssue.Category.COMPLAINT,
        severity=TripIssue.Severity.HIGH,
        note="Late",
        user=UserFactory(),
    )

    assert _row(lead).open_issues == 1
    assert _row(lead, issues="1") is not None


# --- filters, counts, sort -----------------------------------------------------------


def test_stage_and_balance_filters():
    owed = _order(collected="0")
    settled = _order(collected="400")

    assert _row(owed, balance="settled") is None
    assert _row(settled, balance="settled") is not None
    assert _row(owed, balance="owed") is not None
    assert _row(owed, stage="billing") is None
    assert _row(owed, stage="needs_review") is not None


def test_the_finished_window():
    recent = _order(days_ago=2)
    Task.objects.filter(lead=recent, kind="ops_review").update(
        created_at=timezone.now() - timedelta(days=45)
    )

    assert _row(recent) is None  # default window is 30 days
    assert _row(recent, finished="90") is not None
    assert _row(recent, finished="all") is not None


def test_counts_over_the_window():
    a = _order(collected="0")
    b = _order(collected="400")
    _review(b.reservations.get())

    counts = review_queue.counts(review_queue.Filters())

    assert counts.in_review == 2
    assert counts.needs_review == 1
    assert counts.billing == 1
    assert counts.owed == Decimal("400.00")
    assert a  # listed


def test_sorted_by_stage_then_oldest_finish():
    billing = _order(days_ago=5)
    _review(billing.reservations.get())
    newer = _order(days_ago=1)
    older = _order(days_ago=3)

    rows = review_queue.orders_in_review(review_queue.Filters())

    assert [r.lead.pk for r in rows] == [older.pk, newer.pk, billing.pk]


def test_finished_at_is_in_the_last_trips_zone():
    lead = _order(trips=1)

    row = _row(lead)

    assert row.finished_display.endswith(("EDT", "EST"))


def test_the_list_costs_the_same_for_three_orders_or_twelve():
    for _ in range(3):
        _order(trips=2)
    with CaptureQueriesContext(connection) as few:
        review_queue.orders_in_review(review_queue.Filters())
    for _ in range(9):
        _order(trips=2)
    with CaptureQueriesContext(connection) as many:
        rows = review_queue.orders_in_review(review_queue.Filters())

    assert len(rows) == 12
    assert len(many) == len(few)


def test_order_review_for_one_order():
    lead = _order(trips=2, collected="100")

    row = review_queue.order_review(lead.pk)

    assert row.lead == lead
    assert len(row.trips) == 2
    assert row.trips[0].pickup_stop is not None
