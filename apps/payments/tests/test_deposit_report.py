"""Daily internal report: booked orders with an unpaid deposit and a trip inside the
balance window (spec 2026-08-29 §7)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.urls import reverse

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import reports
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.payments.tasks import send_unpaid_deposit_report
from apps.reservations.factories import TransferReservationFactory

pytestmark = pytest.mark.django_db

TODAY = date(2026, 9, 1)
WINDOW = settings.BALANCE_CHARGE_DAYS_BEFORE


def _order(days_out: int, *, deposit=PaymentPlan.DepositStatus.UNSENT, plan=True, **lead_kw):
    lead = LeadFactory(status=Lead.Status.BOOKED, **lead_kw)
    TransferReservationFactory(
        lead=lead, rate=Decimal("400.00"), pickup_date=TODAY + timedelta(days=days_out)
    )
    if plan:
        PaymentPlanFactory(lead=lead, quote_total=Decimal("400.00"), deposit_status=deposit)
    return lead


def test_lists_unpaid_orders_with_a_trip_inside_the_window():
    inside = _order(WINDOW)
    _order(WINDOW + 1)
    rows = reports.unpaid_deposit_rows(today=TODAY)
    assert [r["lead"].pk for r in rows] == [inside.pk]


def test_excludes_paid_deposits_and_quotes():
    _order(5, deposit=PaymentPlan.DepositStatus.PAID)
    quoted = LeadFactory(status=Lead.Status.QUOTED)
    TransferReservationFactory(lead=quoted, pickup_date=TODAY + timedelta(days=5))
    assert reports.unpaid_deposit_rows(today=TODAY) == []


def test_includes_a_booked_order_that_never_got_a_plan():
    lead = _order(5, plan=False)
    rows = reports.unpaid_deposit_rows(today=TODAY)
    assert [r["lead"].pk for r in rows] == [lead.pk]
    assert rows[0]["total"] == Decimal("400.00")


def test_overdue_first_and_days_out_is_signed():
    soon = _order(3)
    overdue = _order(-2)
    rows = reports.unpaid_deposit_rows(today=TODAY)
    assert [r["lead"].pk for r in rows] == [overdue.pk, soon.pk]
    assert rows[0]["days_out"] == -2 and rows[1]["days_out"] == 3


def test_row_carries_customer_contact_trips_money_and_link(settings):
    settings.PUBLIC_BASE_URL = "https://portal.example.com/"
    contact = ContactFactory(name="Ada Kavanagh", email="ada@example.com", phone="+12025550143")
    lead = _order(5, contact=contact)
    TransferReservationFactory(
        lead=lead, rate=Decimal("100.00"), pickup_date=TODAY + timedelta(days=9)
    )
    row = reports.unpaid_deposit_rows(today=TODAY)[0]
    assert row["quote_no"] == lead.quote_no
    assert (row["customer"], row["email"], row["phone"]) == (
        "Ada Kavanagh",
        "ada@example.com",
        "+12025550143",
    )
    assert row["trips"] == 2
    assert row["earliest_pickup"] == TODAY + timedelta(days=5)
    assert row["collected"] == Decimal("0.00")
    assert row["url"] == "https://portal.example.com" + reverse("lead_detail", args=[lead.pk])


def test_nothing_is_sent_on_an_empty_day(mailoutbox):
    assert send_unpaid_deposit_report(today=TODAY) == 0
    assert mailoutbox == []


def test_one_email_per_recipient_with_the_orders(mailoutbox, settings):
    settings.DEPOSIT_REPORT_EMAILS = ["ops@example.com", "owner@example.com"]
    lead = _order(4, contact=ContactFactory(name="Ada Kavanagh", email="ada@example.com"))
    assert send_unpaid_deposit_report(today=TODAY) == 1
    assert sorted(m.to[0] for m in mailoutbox) == ["ops@example.com", "owner@example.com"]
    body = mailoutbox[0].body
    assert lead.quote_no in body and "Ada Kavanagh" in body and "ada@example.com" in body
    assert "$400.00" in body and "1 trip" in body
    assert "1 order" in mailoutbox[0].subject


def test_empty_recipient_list_sends_nothing_and_warns(mailoutbox, settings, caplog):
    settings.DEPOSIT_REPORT_EMAILS = []
    _order(4)
    with caplog.at_level("WARNING"):
        assert send_unpaid_deposit_report(today=TODAY) == 1
    assert mailoutbox == []
    assert "DEPOSIT_REPORT_EMAILS is empty" in caplog.text
