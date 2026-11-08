"""Daily office report of trips the customer hasn't confirmed (APC-19).

The T-72h and T-48h notices are the automated asks; whatever is still unconfirmed the day
before pickup lands here and gets confirmed by hand.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging.models import MessageTemplate
from apps.reservations import reports, tasks
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db

# The clock is pinned at noon so "tomorrow" is the same day whenever the suite runs (APC-116).
NOON = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
TOMORROW = NOON.date() + timedelta(days=1)


@pytest.fixture(autouse=True)
def _noon():
    with patch("django.utils.timezone.now", return_value=NOON):
        yield


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


def test_a_wedding_row_names_the_billing_contact_who_was_asked():
    from apps.leads.factories import ServiceTypeFactory

    payer = ContactFactory(name="Pat Payer", phone="+15715550002")
    trip = _trip(service_type=ServiceTypeFactory(name="Wedding Transportation"))
    Lead.objects.filter(pk=trip.lead_id).update(billing_contact=payer)

    (row,) = reports.unconfirmed_trip_rows()

    assert (row["customer"], row["phone"]) == ("Pat Payer", "+15715550002")


def test_a_held_order_row_names_the_billing_contact_who_was_asked():
    """APC-95: a held order (a vehicle with a cleaning fee) is confirmed by its billing
    contact, wedding or not; the chase list must name who got the link."""
    from decimal import Decimal

    from apps.contacts.factories import CompanyFactory
    from apps.leads.factories import VehicleTypeFactory

    ap = ContactFactory(name="Avery AP", phone="+15715550009")
    company = CompanyFactory(billing_contact=ap)
    booker = ContactFactory(name="Bo Booker", company=company)
    van = VehicleTypeFactory(name="Sprinter chase", cleaning_fee=Decimal("250"))
    _trip(lead=LeadFactory(contact=booker, status=Lead.Status.BOOKED), vehicle=van)

    (row,) = reports.unconfirmed_trip_rows()

    assert (row["customer"], row["phone"]) == ("Avery AP", "+15715550009")


def test_an_unheld_company_order_row_names_the_booker():
    from apps.contacts.factories import CompanyFactory

    company = CompanyFactory(billing_contact=ContactFactory(name="Avery AP"))
    booker = ContactFactory(name="Bo Booker", company=company)
    _trip(lead=LeadFactory(contact=booker, status=Lead.Status.BOOKED))

    (row,) = reports.unconfirmed_trip_rows()

    assert row["customer"] == "Bo Booker"


def test_report_emails_the_office():
    _trip()

    count = tasks.send_unconfirmed_trip_report()

    assert count == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["reservations@allprocharter.com"]


def test_nothing_goes_out_on_a_quiet_day():
    MessageTemplate.objects.filter(key="unconfirmed_trips_report").update(
        staff_emails=["reservations@allprocharter.com"]
    )
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


def test_held_rows_do_not_scale_queries_either(django_assert_num_queries):
    from decimal import Decimal

    from apps.contacts.factories import CompanyFactory
    from apps.leads.factories import VehicleTypeFactory

    company = CompanyFactory(billing_contact=ContactFactory(name="Avery AP"))
    van = VehicleTypeFactory(name="Sprinter chase", cleaning_fee=Decimal("250"))
    for i in range(4):
        booker = ContactFactory(name=f"Booker {i}", company=company)
        _trip(lead=LeadFactory(contact=booker, status=Lead.Status.BOOKED), vehicle=van)

    with django_assert_num_queries(2):  # still the trips + their stops
        rows = reports.unconfirmed_trip_rows()

    assert [r["customer"] for r in rows] == ["Avery AP"]
    assert len(rows[0]["trips"]) == 4
