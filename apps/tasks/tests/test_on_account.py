"""APC-105: an order billed to an account owes no deposit and no card balance, so the two
money tasks don't apply to it — and come back when it goes back to card."""

from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing import services as billing
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import services
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db
MONEY = ("deposit_received", "final_balance_paid")


@pytest.fixture(autouse=True)
def _quiet():
    with (
        patch("apps.integrations.la_sync.push_lead_bookings"),
        patch("apps.messaging.touchpoints.notify_status_change"),
    ):
        yield


def _order():
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=10),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
        hours=Decimal("1"),
    )
    group = billing.create_account(
        lead.contact, name="Ridgeline", group_name="Accounts Payable"
    ).groups.get()
    return lead, group


def _statuses(lead) -> dict:
    return dict(Task.objects.filter(lead=lead, kind__in=MONEY).values_list("kind", "status"))


def test_an_account_order_never_gets_the_money_tasks():
    lead, group = _order()
    billing.bill_to_account(lead, group, po_number="", user=None)

    services.ensure_tasks(Lead.objects.get(pk=lead.pk))

    assert _statuses(lead) == {}
    assert Task.objects.filter(lead=lead, kind="contract_signed").exists()


def test_open_money_tasks_close_as_not_applicable_on_the_switch(django_capture_on_commit_callbacks):
    lead, group = _order()
    services.ensure_tasks(lead)
    assert set(_statuses(lead).values()) == {Task.Status.OPEN}

    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_account(lead, group, po_number="", user=None)

    assert set(_statuses(lead).values()) == {Task.Status.NOT_APPLICABLE}


def test_back_to_card_reopens_them(django_capture_on_commit_callbacks):
    lead, group = _order()
    services.ensure_tasks(lead)
    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_account(lead, group, po_number="", user=None)

    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_card(lead, user=None)

    assert _statuses(lead) == {k: Task.Status.OPEN for k in MONEY}
    assert all(t.completed_at is None for t in Task.objects.filter(lead=lead, kind__in=MONEY))


def test_a_task_a_person_closed_is_not_reopened(django_capture_on_commit_callbacks):
    lead, group = _order()
    services.ensure_tasks(lead)
    deposit = Task.objects.get(lead=lead, kind="deposit_received")
    services.skip(deposit, user=None, note="Paid by check at the office")
    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_account(lead, group, po_number="", user=None)
    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_card(lead, user=None)

    assert _statuses(lead)["deposit_received"] == Task.Status.SKIPPED


def test_back_to_card_leaves_a_cancelled_orders_tasks_closed(django_capture_on_commit_callbacks):
    from apps.reservations.models import Reservation

    lead, group = _order()
    services.ensure_tasks(lead)
    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_account(lead, group, po_number="", user=None)
    lead.reservations.update(trip_status=Reservation.TripStatus.CANCELLED)

    with django_capture_on_commit_callbacks(execute=True):
        billing.bill_to_card(lead, user=None)

    assert set(_statuses(lead).values()) == {Task.Status.NOT_APPLICABLE}
