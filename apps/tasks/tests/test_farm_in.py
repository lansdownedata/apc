"""APC-92 — farmed-in orders: the sending operator pays, on its own clock.

Client, 2026-10-19: the deposit is due 3 days before the trip (not 3 days after booking)
and the final balance within 24 hours after the trip is done (not 28 days before).
"""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory
from apps.tasks import services
from apps.tasks.facts import load_facts
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


def _order(*, farm_in: bool, days_out: int = 10, started_ago: timedelta | None = None):
    """A booked one-trip order for a contact at a company; `started_ago` puts the trip in
    the past (1 billed hour) so the order has finished."""
    company = CompanyFactory(is_affiliate_account=farm_in)
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(company=company))
    if started_ago is None:
        day = timezone.localdate() + timedelta(days=days_out)
        when = {"pickup_date": day, "pickup_time": time(9, 0)}
    else:
        start = (timezone.now() - started_ago).astimezone(NY).replace(second=0, microsecond=0)
        when = {"pickup_date": start.date(), "pickup_time": start.time()}
    trip = ReservationFactory(
        lead=lead, pickup_timezone="America/New_York", hours=Decimal("1"), **when
    )
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    return lead, trip


def _task(lead, kind):
    return Task.objects.filter(lead=lead, kind=kind).first()


def _local_midnight(day) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=NY).astimezone(UTC)


def _end(trip) -> datetime:
    """The order's scheduled end: pickup + 1 billed hour, as UTC."""
    trip.refresh_from_db()
    return (trip.pickup_at + timedelta(hours=1)).astimezone(UTC)


def _flag(client, company, on=True):
    client.force_login(UserFactory())
    return client.post(
        reverse("company_update", args=[company.pk]), {"is_affiliate_account": "1" if on else "0"}
    )


def test_facts_know_a_farmed_in_order():
    farm, _ = _order(farm_in=True)
    plain, _ = _order(farm_in=False)

    facts = load_facts([farm, plain])

    assert facts[farm.pk].is_farm_in is True
    assert facts[plain.pk].is_farm_in is False


def test_a_farmed_in_deposit_is_due_three_days_before_the_trip():
    lead, trip = _order(farm_in=True, days_out=10)

    services.ensure_tasks(lead)

    due = _task(lead, "deposit_received").due_at
    assert due == _local_midnight(trip.pickup_date - timedelta(days=3))


def test_a_regular_deposit_is_still_due_three_days_after_booking():
    lead, _trip = _order(farm_in=False)

    services.ensure_tasks(lead)

    task = _task(lead, "deposit_received")
    assert task.due_at == task.opens_at + timedelta(hours=72)


def test_a_farmed_in_balance_opens_at_the_scheduled_end_and_is_due_a_day_later():
    lead, trip = _order(farm_in=True, days_out=10)

    services.ensure_tasks(lead)

    task = _task(lead, "final_balance_paid")
    assert task.status == Task.Status.SCHEDULED
    assert task.opens_at == _end(trip)
    assert task.due_at == _end(trip) + timedelta(hours=24)


def test_a_finished_farmed_in_order_has_its_balance_open_and_due_a_day_after_the_end():
    lead, trip = _order(farm_in=True, started_ago=timedelta(hours=4))

    services.ensure_tasks(lead)

    task = _task(lead, "final_balance_paid")
    assert task.status == Task.Status.OPEN
    assert task.due_at == _end(trip) + timedelta(hours=24)


def test_flagging_a_company_redates_its_booked_orders(client):
    lead, trip = _order(farm_in=False, days_out=40)
    services.ensure_tasks(lead)

    resp = _flag(client, lead.contact.company)

    assert resp.status_code == 200
    lead.contact.company.refresh_from_db()
    assert lead.contact.company.is_affiliate_account is True
    deposit = _task(lead, "deposit_received")
    assert deposit.due_at == _local_midnight(trip.pickup_date - timedelta(days=3))
    balance = _task(lead, "final_balance_paid")
    assert (balance.opens_at, balance.due_at) == (_end(trip), _end(trip) + timedelta(hours=24))


def test_flagging_keeps_the_balance_task_on_an_order_that_finished_long_ago(client):
    """Review 2026-10-24 (critical): the flip must never lose an unpaid balance task."""
    lead, trip = _order(farm_in=False, started_ago=timedelta(days=10))
    services.ensure_tasks(lead)
    assert _task(lead, "final_balance_paid") is not None

    _flag(client, lead.contact.company)

    balance = _task(lead, "final_balance_paid")
    assert balance is not None
    # Never born overdue by days: the due date is pulled up to when the task opened.
    assert balance.due_at == max(_end(trip) + timedelta(hours=24), balance.opens_at)


def test_flagging_a_recently_finished_order_dates_the_balance_from_its_end(client):
    """The regular task opened at T-30d; the farm-in due date must not inherit that."""
    lead, trip = _order(farm_in=False, started_ago=timedelta(days=2))
    services.ensure_tasks(lead)
    Task.objects.filter(lead=lead, kind="final_balance_paid").update(
        status=Task.Status.OPEN, opens_at=timezone.now() - timedelta(days=32)
    )

    _flag(client, lead.contact.company)

    assert _task(lead, "final_balance_paid").due_at == _end(trip) + timedelta(hours=24)


def test_flagging_creates_no_tasks_on_old_orders_that_never_had_any(client):
    lead, _trip = _order(farm_in=False, started_ago=timedelta(days=60))
    assert not Task.objects.filter(lead=lead).exists()

    _flag(client, lead.contact.company)

    assert not Task.objects.filter(lead=lead).exists()


def test_moving_a_contact_into_an_affiliate_company_redates_their_orders(client):
    lead, trip = _order(farm_in=False, days_out=40)
    services.ensure_tasks(lead)
    operator = CompanyFactory(name="Operator Co", is_affiliate_account=True)
    client.force_login(UserFactory())

    client.post(reverse("contact_update", args=[lead.contact.pk]), {"company": operator.name})

    deposit = _task(lead, "deposit_received")
    assert deposit.due_at == _local_midnight(trip.pickup_date - timedelta(days=3))


def test_flagging_never_reopens_a_task_a_person_closed(client):
    lead, _trip = _order(farm_in=False, days_out=40)
    services.ensure_tasks(lead)
    deposit = _task(lead, "deposit_received")
    services.complete(deposit, UserFactory(), note="Paid by check")
    client.force_login(UserFactory())

    client.post(
        reverse("company_update", args=[lead.contact.company.pk]), {"is_affiliate_account": "1"}
    )

    deposit.refresh_from_db()
    assert deposit.status == Task.Status.DONE


def test_unflagging_restores_the_regular_balance_dates(client):
    lead, trip = _order(farm_in=True, days_out=40)
    services.ensure_tasks(lead)

    _flag(client, lead.contact.company, on=False)

    balance = _task(lead, "final_balance_paid")
    assert balance.opens_at == _local_midnight(trip.pickup_date - timedelta(days=30))
    assert balance.due_at == _local_midnight(trip.pickup_date - timedelta(days=28))


def test_the_flag_shows_on_the_company_contact_and_order(client):
    lead, _trip = _order(farm_in=True)
    client.force_login(UserFactory())
    company = lead.contact.company

    company_html = client.get(reverse("company_detail", args=[company.pk])).content.decode()
    contact_html = client.get(reverse("contact_detail", args=[lead.contact.pk])).content.decode()
    order_html = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert 'name="is_affiliate_account"' in company_html
    assert "Affiliate account" in contact_html
    assert "Affiliate account" in order_html


def test_a_regular_order_shows_no_affiliate_chip(client):
    lead, _trip = _order(farm_in=False)
    client.force_login(UserFactory())

    order_html = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    assert "Affiliate account" not in order_html
