"""Daily office report of trips the customer hasn't confirmed (APC-19).

The T-72h and T-48h notices are the automated asks; whatever is still unconfirmed the day
before pickup lands here and gets confirmed by hand.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.test import override_settings
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations import reports, tasks
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

TOMORROW = timezone.localdate() + timedelta(days=1)


def _trip(**kw):
    kw.setdefault("pickup_date", TOMORROW)
    lead = kw.pop("lead", None) or LeadFactory(status=Lead.Status.BOOKED)
    return ReservationFactory(lead=lead, **kw)


def test_rows_list_tomorrows_unconfirmed_trips():
    res = _trip()

    rows = reports.unconfirmed_trip_rows()

    assert len(rows) == 1
    assert rows[0]["customer"] == res.lead.contact.name
    assert [t["reservation"] for t in rows[0]["trips"]] == [res]


def test_rows_exclude_confirmed_trips():
    _trip(customer_confirmed_at=timezone.now())

    assert reports.unconfirmed_trip_rows() == []


def test_rows_exclude_cancelled_trips_and_unbooked_orders():
    _trip(trip_status="cancelled")
    ReservationFactory(lead=LeadFactory(status=Lead.Status.QUOTED), pickup_date=TOMORROW)

    assert reports.unconfirmed_trip_rows() == []


def test_rows_cover_tomorrow_only():
    _trip(pickup_date=TOMORROW + timedelta(days=1))
    _trip(pickup_date=timezone.localdate())

    assert reports.unconfirmed_trip_rows() == []


def test_a_customer_with_several_trips_is_one_row():
    contact = ContactFactory()
    first = _trip(lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED))
    second = _trip(lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED))

    rows = reports.unconfirmed_trip_rows()

    assert len(rows) == 1
    assert [t["reservation"] for t in rows[0]["trips"]] == [first, second]


@override_settings(TRIP_CONFIRM_REPORT_EMAILS=["reservations@allprocharter.com"])
def test_report_emails_the_office():
    _trip()

    count = tasks.send_unconfirmed_trip_report()

    assert count == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["reservations@allprocharter.com"]


@override_settings(TRIP_CONFIRM_REPORT_EMAILS=["reservations@allprocharter.com"])
def test_nothing_goes_out_on_a_quiet_day():
    assert tasks.send_unconfirmed_trip_report() == 0
    assert mail.outbox == []


def test_the_job_is_registered_for_cron():
    from apps.core.cron import JOBS

    assert JOBS["unconfirmed-trips-report"] is tasks.send_unconfirmed_trip_report


def test_rows_do_not_scale_queries_with_the_number_of_trips(django_assert_num_queries):
    """`Reservation.pickup`/`dropoff` bypass the prefetch — the route is built off it."""
    contact = ContactFactory()
    for _ in range(4):
        _trip(lead=LeadFactory(contact=contact, status=Lead.Status.BOOKED))

    with django_assert_num_queries(2):  # the trips + their prefetched stops
        rows = reports.unconfirmed_trip_rows()

    assert len(rows[0]["trips"]) == 4
