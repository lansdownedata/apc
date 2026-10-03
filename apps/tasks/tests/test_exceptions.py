"""APC-69 — the exception dashboard's numbers: each count matches its source, and a daily
snapshot keeps the trend."""

from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchException
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import payables
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import AffiliatePayable, PaymentPlan
from apps.payments.reports import unpaid_deposit_rows
from apps.reservations import reviews
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import TripIssue
from apps.tasks import exceptions, services
from apps.tasks.jobs import run_tasks
from apps.tasks.models import ExceptionSnapshot, Task, TaskConfig

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order(*, days=10, deposit_paid=True, farmed_out=False):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=days),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    if deposit_paid:
        PaymentPlanFactory(lead=lead, deposit_status=PaymentPlan.DepositStatus.PAID)
    if farmed_out:
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    return lead


def _open(lead, kind, *, overdue=False, department=None):
    """One open task of `kind` on the order (created if the engine hasn't yet)."""
    task = Task.objects.filter(lead=lead, kind=kind).first() or Task(
        lead=lead, kind=kind, department=department or "operations", opens_at=timezone.now()
    )
    task.status = Task.Status.OPEN
    if department:
        task.department = department
    shift = timedelta(hours=-3) if overdue else timedelta(days=3)
    task.due_at = timezone.now() + shift
    task.save()
    return task


def _quiet_board():
    """Close everything the fixtures opened, so a test counts only what it sets up."""
    Task.objects.update(status=Task.Status.DONE)


# --- counts ----------------------------------------------------------------------------


def test_every_view_is_counted_and_starts_at_zero():
    counts = exceptions.counts()

    assert set(counts) == {v.key for v in exceptions.VIEWS}
    assert set(counts.values()) == {0}


def test_dispatch_views_count_open_exceptions_by_kind():
    K = DispatchException.Kind
    trips = [_order().reservations.get() for _ in range(4)]
    DispatchException.objects.create(reservation=trips[0], kind=K.UNASSIGNED)
    DispatchException.objects.create(reservation=trips[1], kind=K.UNASSIGNED)
    DispatchException.objects.create(reservation=trips[2], kind=K.AFFILIATE_UNACKED)
    DispatchException.objects.create(reservation=trips[3], kind=K.NO_DRIVER_INFO)
    DispatchException.objects.create(
        reservation=trips[3], kind=K.UNASSIGNED, resolved_at=timezone.now()
    )

    counts = exceptions.counts()

    assert counts["no_coverage"] == 2
    assert counts["affiliate_unconfirmed"] == 1
    assert counts["no_driver_info"] == 1


def test_unpaid_deposits_match_the_deposit_report():
    _order(deposit_paid=False)
    _order(deposit_paid=False)
    _order(deposit_paid=True)
    _order(deposit_paid=False, days=90)  # outside the report's window

    assert exceptions.counts()["unpaid_deposits"] == len(unpaid_deposit_rows()) == 2


def test_task_views_count_open_tasks_of_their_kinds():
    a, b = _order(), _order()
    for lead in (a, b):
        services.ensure_tasks(lead)
    _quiet_board()
    _open(a, "day_of_contact")
    _open(a, "wedding_names")
    _open(b, "wedding_names")
    _open(a, "ops_review")
    _open(b, "overtime_invoiced")
    _open(a, "final_balance_paid", overdue=True)
    _open(b, "final_balance_paid")  # open but not past due: not an exception yet

    counts = exceptions.counts()

    assert counts["missing_day_of_contact"] == 1
    assert counts["missing_wedding_names"] == 2
    assert counts["overtime_awaiting"] == 2
    assert counts["unpaid_balances"] == 1


def test_overdue_tasks_are_counted_by_department():
    lead = _order()
    _open(lead, "deposit_received", overdue=True, department="customer_service")
    _open(lead, "final_itinerary", overdue=True, department="customer_service")
    _open(lead, "details_finalized", overdue=True, department="operations")
    _open(lead, "contract_signed", department="customer_service")  # not overdue

    counts = exceptions.counts()

    assert counts["overdue_customer_service"] == 2
    assert counts["overdue_operations"] == 1
    assert counts["overdue_accounting"] == 0


def test_payable_views_follow_the_payable_stage():
    admin = UserFactory(role="owner_admin", can_manage_payments=True)
    for _ in range(3):
        payables.ensure_payables(_order(farmed_out=True, days=-1))
    rows = list(AffiliatePayable.objects.order_by("pk"))
    payables.record_invoice(rows[1], user=admin, number="INV-1", amount=rows[1].expected_amount)
    payables.record_invoice(rows[2], user=admin, number="INV-2", amount=rows[2].expected_amount)
    payables.approve(AffiliatePayable.objects.get(pk=rows[2].pk), user=admin)

    counts = exceptions.counts()

    assert counts["missing_invoices"] == 1
    assert counts["pending_payments"] == 1


def test_open_trip_issues_are_counted():
    lead = _order(days=-1)
    review = reviews.review_for(lead.reservations.get())
    for _ in range(2):
        reviews.add_issue(
            review,
            category=TripIssue.Category.SERVICE,
            severity=TripIssue.Severity.LOW,
            note="x",
            user=UserFactory(),
        )
    TripIssue.objects.filter(pk=TripIssue.objects.first().pk).update(resolved_at=timezone.now())

    assert exceptions.counts()["open_issues"] == 1


def test_the_counts_cost_the_same_however_much_there_is():
    _order(deposit_paid=False)
    with CaptureQueriesContext(connection) as few:
        exceptions.counts()
    for _ in range(12):
        lead = _order(deposit_paid=False, farmed_out=True, days=-1)
        services.ensure_tasks(lead)
        DispatchException.objects.create(
            reservation=lead.reservations.get(), kind=DispatchException.Kind.UNASSIGNED
        )
    with CaptureQueriesContext(connection) as many:
        exceptions.counts()

    assert len(many) == len(few) <= 8


# --- the daily snapshot ----------------------------------------------------------------


def test_run_tasks_writes_one_snapshot_per_business_day():
    _order(deposit_paid=False)

    run_tasks()
    run_tasks()

    today = timezone.localdate()
    rows = ExceptionSnapshot.objects.filter(day=today)
    assert rows.count() == len(exceptions.VIEWS)
    assert rows.get(key="unpaid_deposits").count == 1
    assert TaskConfig.load().exceptions_snapshot_on == today


def test_a_later_tick_the_same_day_does_not_rewrite_it():
    run_tasks()
    _order(deposit_paid=False)

    run_tasks()

    assert ExceptionSnapshot.objects.get(key="unpaid_deposits").count == 0


def test_the_next_business_day_gets_its_own_snapshot():
    run_tasks()
    tomorrow = timezone.now() + timedelta(days=1)

    with patch("django.utils.timezone.now", return_value=tomorrow):
        run_tasks()

    assert ExceptionSnapshot.objects.filter(key="unpaid_deposits").count() == 2


def test_the_trend_is_the_last_thirty_days_oldest_first():
    today = timezone.localdate()
    for back, n in ((40, 9), (2, 3), (1, 5)):
        ExceptionSnapshot.objects.create(
            day=today - timedelta(days=back), key="unpaid_deposits", count=n
        )

    trend = exceptions.trend()

    assert [n for _, n in trend["unpaid_deposits"]] == [3, 5]
    assert trend["no_coverage"] == []


# --- drill-down ------------------------------------------------------------------------


def test_a_task_view_drills_into_the_queue_with_its_filters():
    views = {v.key: v for v in exceptions.VIEWS}
    queue = reverse("task_queue")

    assert (
        views["unpaid_balances"].url
        == f"{queue}?kind=final_balance_paid&due=overdue&assignee=anyone"
    )
    assert (
        views["overdue_accounting"].url
        == f"{queue}?department=accounting&due=overdue&assignee=anyone"
    )
    assert views["open_issues"].url == f"{reverse('trip_review_list')}?issues=1"


def test_the_queue_shows_exactly_what_an_exact_view_counted(client):
    me = UserFactory()
    client.force_login(me)
    lead = _order()
    _open(lead, "final_itinerary", overdue=True, department="customer_service")
    _open(lead, "deposit_received", overdue=True, department="customer_service")
    _open(lead, "contract_signed", department="customer_service")
    view = next(v for v in exceptions.VIEWS if v.key == "overdue_customer_service")

    rows = client.get(view.url).context["rows"]

    assert len(rows) == exceptions.counts()["overdue_customer_service"] == 2


# --- the page and the summary card -----------------------------------------------------


def _seed():
    lead = _order()
    _open(lead, "final_balance_paid", overdue=True, department="accounting")
    _open(lead, "wedding_names", department="customer_service")
    DispatchException.objects.create(
        reservation=lead.reservations.get(), kind=DispatchException.Kind.UNASSIGNED
    )
    today = timezone.localdate()
    for back, n in ((7, 3), (1, 2)):
        ExceptionSnapshot.objects.get_or_create(
            day=today - timedelta(days=back), key="no_coverage", defaults={"count": n}
        )
    return lead


def test_the_dashboard_leads_with_the_number_and_sorts_worst_first():
    _seed()
    lead = _order()
    _open(lead, "wedding_names", department="customer_service")

    data = exceptions.dashboard()

    # No coverage 1 + wedding names 2 + unpaid balance 1; overdue-by-department restates.
    assert data["total"] == 4
    assert data["delta"] == 1  # a week ago the snapshots summed to 3
    assert [g["label"] for g in data["groups"]] == [
        "Dispatch",
        "Customer details",
        "Money",
        "Overdue tasks",
    ]
    customer = data["groups"][1]
    assert [(r["label"], r["count"]) for r in customer["rows"]] == [("Wedding names missing", 2)]
    assert "Day-of contact missing" in data["quiet"]
    assert "Overdue: Sales" in data["quiet"]
    dispatch = data["groups"][0]["rows"][0]
    assert dispatch["delta"] == -2 and dispatch["points"]


def test_the_page_renders_rows_that_link_to_the_drill_down(client):
    _seed()
    client.force_login(UserFactory())

    body = client.get(reverse("exceptions")).content.decode()

    assert "Exceptions" in body and "Wedding names missing" in body
    assert "kind=final_balance_paid&amp;due=overdue&amp;assignee=anyone" in body
    assert "Nothing open:" in body and "Day-of contact missing" in body
    assert "See all" in body  # the Overdue tasks heading


def test_the_page_with_nothing_wrong(client):
    client.force_login(UserFactory())

    body = client.get(reverse("exceptions")).content.decode()

    assert "Nothing needs attention." in body
    assert "Nothing open:" not in body


def test_the_page_needs_a_login(client):
    assert client.get(reverse("exceptions")).status_code == 302


def test_the_summary_card_is_on_the_dashboard(client):
    _seed()
    client.force_login(UserFactory())

    body = client.get(reverse("dashboard")).content.decode()

    assert "data-exceptions-summary" in body
    assert "Wedding names missing" in body
    assert reverse("exceptions") in body


def test_the_page_costs_the_same_however_much_is_wrong(client):
    client.force_login(UserFactory())
    _seed()
    with CaptureQueriesContext(connection) as few:
        client.get(reverse("exceptions"))
    for _ in range(10):
        _seed()
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("exceptions"))

    assert len(many) == len(few)
