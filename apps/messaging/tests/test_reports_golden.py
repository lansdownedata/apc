"""Golden recording of the staff reports: what each report job emails today, word for word,
and to whom (APC-120).

Recorded once on the report jobs as they stood before they moved onto message rows (the
unpaid-deposit report, the unconfirmed-trips report, the customer review report, the
complaint report, the overdue-task digest and the dispatch exception alert, whose critical
texts are recorded too). `golden/reports.json` is never re-recorded after that: a failure
here means the move changed what the office receives, or who receives it.

Each scenario builds fixed data on the frozen clock of `golden/scenarios.py` and runs the
report once; the empty scenarios pin that a quiet day still sends nothing. Every send is
captured at the transport: Podium texts, and the recipient, subject and text part of every
email handed to Django's mail backend, compared whitespace-normalised.

Record (once only): GOLDEN_RECORD=1 pytest apps/messaging/tests/test_reports_golden.py
"""

import json
import os
import re
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.dispatch import monitoring
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.leads.factories import LeadFactory
from apps.leads.models import CustomerFeedback, Lead
from apps.messaging.factories import ReviewFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.payments.tasks import send_unpaid_deposit_report
from apps.reservations import reviews
from apps.reservations import tasks as reservation_tasks
from apps.reservations.factories import ReservationFactory, TransferReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.tasks import escalation
from apps.tasks import reports as task_reports
from apps.tasks import services as task_services
from apps.tasks.models import Task, TaskConfig
from apps.vendors.factories import VendorFactory

from .golden import scenarios as s

pytestmark = pytest.mark.django_db

EXPECTED = Path(__file__).parent / "golden" / "reports.json"
RECORD = os.environ.get("GOLDEN_RECORD") == "1"
SEND = "apps.integrations.podium.send_message"
# A trip number printed outside a link's path ("trip #57", "?trip=57").
_NUMBER = re.compile(r"(#|\?trip=)\d+\b")

DEPOSIT_TO = ["ops@allprocharter.com", "owner@allprocharter.com"]
UNCONFIRMED_TO = ["reservations@allprocharter.com"]
DIGEST_TO = ["ops@allprocharter.com"]
REPORTS_TO = ["owner@allprocharter.com"]
DISPATCH_TO = ["dispatch@allprocharter.com", "night@allprocharter.com"]
CRITICAL_SMS = "+15715550100, +15715550101"


@pytest.fixture(autouse=True)
def _live(settings):
    settings.TOUCHPOINTS_ENABLED = False
    # Staff reports go out while the go-live switch is still off, as they always have.
    settings.OUTBOUND_ENABLED = False
    settings.PUBLIC_BASE_URL = s.BASE_URL
    settings.TIME_ZONE = "America/New_York"
    settings.COMPANY_NAME = "All Pro Charter"
    settings.COMPANY_PHONE = "(703) 555-0100"
    settings.COMPANY_EMAIL = "reservations@allprocharter.com"
    settings.BALANCE_CHARGE_DAYS_BEFORE = 30
    _recipients(settings)


def _recipients(settings) -> None:
    """Who each report goes to, set wherever the list lives: before APC-120 the two env
    settings and the Tasks and Dispatch alerts fields, after it each report's message row.
    Both are set, so one recording holds across the move."""
    settings.DEPOSIT_REPORT_EMAILS = DEPOSIT_TO
    settings.TRIP_CONFIRM_REPORT_EMAILS = UNCONFIRMED_TO
    tasks = TaskConfig.load()
    tasks.digest_emails = "\n".join(DIGEST_TO)
    tasks.report_emails = "\n".join(REPORTS_TO)
    tasks.save()
    dispatch = DispatchAlertConfig.load()
    dispatch.alert_emails = "\n".join(DISPATCH_TO)
    dispatch.critical_sms = CRITICAL_SMS
    dispatch.save()
    rows = {
        "deposit_report": DEPOSIT_TO,
        "unconfirmed_trips_report": UNCONFIRMED_TO,
        "review_report": REPORTS_TO,
        "complaint_report": REPORTS_TO,
        "task_digest": DIGEST_TO,
        "dispatch_alert": DISPATCH_TO,
    }
    for key, emails in rows.items():
        MessageTemplate.objects.filter(key=key).update(staff_emails=emails)


# --- data ------------------------------------------------------------------------------


def _customer(name: str, email: str = "", phone: str = ""):
    return ContactFactory(name=name, email=email, phone=phone)


def _today():
    return timezone.localdate()


def _booked(person, **lead_kw) -> Lead:
    return LeadFactory(status=Lead.Status.BOOKED, contact=person, **lead_kw)


def _unpaid(person, *days_out: int, rate="400.00") -> Lead:
    lead = _booked(person)
    for days in days_out:
        TransferReservationFactory(
            lead=lead, rate=Decimal(rate), pickup_date=_today() + timedelta(days=days)
        )
    PaymentPlanFactory(
        lead=lead,
        quote_total=Decimal(rate) * len(days_out),
        deposit_status=PaymentPlan.DepositStatus.UNSENT,
    )
    return lead


def _tomorrow_trip(lead: Lead, at: time, route=("1 Main St, Washington, DC", "Dulles (IAD)")):
    return ReservationFactory(
        lead=lead,
        pickup_date=_today() + timedelta(days=1),
        pickup_time=at,
        pickup_timezone="America/New_York",
        stops=list(route),
    )


def _finished_order(name: str, *, days_ago: int = 3) -> Lead:
    lead = _booked(_customer(name, f"{name.split()[0].lower()}@example.com", "+12025550160"))
    ReservationFactory(
        lead=lead,
        pickup_date=_today() - timedelta(days=days_ago),
        pickup_time=time(10, 0),
        pickup_timezone="America/New_York",
        trip_status=Reservation.TripStatus.DONE,
    )
    return lead


def _rate(lead: Lead, rating: int, comment: str = "") -> None:
    CustomerFeedback.objects.create(
        lead=lead, rating=rating, comment=comment, submitted_at=timezone.now()
    )


def _logger() -> User:
    return UserFactory(username="moe", first_name="Moe", last_name="A")


def _complain(lead: Lead, note: str, category=TripIssue.Category.COMPLAINT, user=None):
    trip = lead.reservations.get()
    return reviews.add_issue(
        reviews.review_for(trip),
        category=category,
        severity=TripIssue.Severity.HIGH,
        note=note,
        user=user,
    )


def _overdue(lead: Lead, kind: str, *, hours: int, assignee=None) -> None:
    now = timezone.now()
    Task.objects.filter(lead=lead, kind=kind).update(
        status=Task.Status.OPEN,
        opens_at=now - timedelta(days=5),
        due_at=now - timedelta(hours=hours),
        assignee=assignee,
    )


def _order_with_tasks(name: str) -> Lead:
    lead = _booked(_customer(name, f"{name.split()[0].lower()}@example.com", "+12025550170"))
    ReservationFactory(
        lead=lead,
        pickup_date=_today() + timedelta(days=10),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    task_services.ensure_tasks(lead)
    Task.objects.filter(lead=lead).update(status=Task.Status.NOT_APPLICABLE)
    return lead


def _trip_in(delta: timedelta, name: str, *, covered: bool = False) -> Reservation:
    when = timezone.localtime() + delta
    lead = _booked(_customer(name, f"{name.split()[0].lower()}@example.com", "+12025550180"))
    trip = ReservationFactory(
        lead=lead,
        pickup_date=when.date(),
        pickup_time=when.time().replace(microsecond=0, second=0),
        pickup_timezone="America/New_York",
    )
    if covered:
        AssignmentFactory(
            reservation=trip,
            vendor=VendorFactory(name="Metro Limo"),
            status=Assignment.Status.CONFIRMED,
            driver_name="Sam Rivera",
            driver_cell="+15715551212",
            affiliate_confirmed_at=timezone.now(),
        )
    return trip


# --- the reports -----------------------------------------------------------------------


def _deposit_report_two_orders():
    _unpaid(_customer("Ada Kavanagh", "ada@example.com", "+12025550143"), 5, 9)
    _unpaid(_customer("Ben Ortiz", "", ""), -2)
    assert send_unpaid_deposit_report() == 2


def _deposit_report_one_order():
    _unpaid(_customer("Ada Kavanagh", "ada@example.com", "+12025550143"), 0)
    assert send_unpaid_deposit_report() == 1


def _deposit_report_empty():
    _unpaid(_customer("Ada Kavanagh", "ada@example.com"), 45)
    assert send_unpaid_deposit_report() == 0


def _unconfirmed_two_customers():
    ada = _customer("Ada Kavanagh", "ada@example.com", "+12025550143")
    _tomorrow_trip(_booked(ada), time(9, 30))
    _tomorrow_trip(_booked(ada), time(17, 0), ("Dulles (IAD)", "The Hay-Adams"))
    _tomorrow_trip(_booked(_customer("Ben Ortiz", "", "+12025550144")), time(11, 15))
    assert reservation_tasks.send_unconfirmed_trip_report() == 2


def _unconfirmed_one_trip():
    _tomorrow_trip(_booked(_customer("Ada Kavanagh", "ada@example.com", "")), time(9, 30))
    assert reservation_tasks.send_unconfirmed_trip_report() == 1


def _unconfirmed_empty():
    trip = _tomorrow_trip(_booked(_customer("Ada Kavanagh", "ada@example.com")), time(9, 30))
    Reservation.objects.filter(pk=trip.pk).update(customer_confirmed_at=timezone.now())
    assert reservation_tasks.send_unconfirmed_trip_report() == 0


def _review_report():
    happy = _finished_order("Happy Customer", days_ago=4)
    ReviewFactory(lead=happy, contact=happy.contact, rating=5, review_site="Google")
    TouchPointFactory(lead=happy, kind=TouchPoint.Kind.REVIEW_REQUEST, status="sent")
    unhappy = _finished_order("Unhappy Customer", days_ago=3)
    _rate(unhappy, 2, "Bus was dirty")
    quiet = _finished_order("Quiet Customer", days_ago=2)
    _rate(quiet, 5)
    assert task_reports.send_review_report() == 3


def _review_report_no_ratings():
    _finished_order("Dana Reyes")
    assert task_reports.send_review_report() == 1


def _review_report_empty():
    assert task_reports.send_review_report() == 0


def _complaint_report_two():
    moe = _logger()
    dana = _finished_order("Dana Reyes", days_ago=0)
    AssignmentFactory(
        reservation=dana.reservations.get(),
        vendor=VendorFactory(name="Metro Limo"),
        status=Assignment.Status.CONFIRMED,
    )
    _complain(dana, "Driver was 40 minutes late", user=moe)
    _complain(dana, "AC was broken", category=TripIssue.Category.VEHICLE, user=moe)
    _complain(_finished_order("Sam Ortiz", days_ago=0), "Wrong pickup spot", user=None)
    assert task_reports.send_complaint_report() == 2


def _complaint_report_one():
    _complain(_finished_order("Dana Reyes", days_ago=0), "Driver was rude", user=_logger())
    assert task_reports.send_complaint_report() == 1


def _complaint_report_empty():
    _complain(
        _finished_order("Dana Reyes", days_ago=0),
        "AC was broken",
        category=TripIssue.Category.VEHICLE,
        user=_logger(),
    )
    assert task_reports.send_complaint_report() == 0


def _escalate():
    UserFactory(username="owner", first_name="Olive", last_name="Owner", role="owner_admin")
    escalation.run(TaskConfig.load(), timezone.now())


def _task_digest_two_departments():
    lead = _order_with_tasks("Dana Reyes")
    jordan = UserFactory(username="jordan", first_name="Jordan", last_name="Ellis")
    _overdue(lead, "final_itinerary", hours=30, assignee=jordan)
    _overdue(lead, "details_finalized", hours=3)
    _escalate()


def _task_digest_one_task():
    _overdue(_order_with_tasks("Dana Reyes"), "final_itinerary", hours=3)
    _escalate()


def _task_digest_nothing_overdue():
    _order_with_tasks("Dana Reyes")
    _escalate()


def _dispatch_alert_critical():
    _trip_in(timedelta(hours=3), "Dana Reyes")
    _trip_in(timedelta(hours=20), "Sam Ortiz")
    monitoring.run_dispatch_monitor()


def _dispatch_alert_warning_only():
    _trip_in(timedelta(hours=20), "Sam Ortiz")
    monitoring.run_dispatch_monitor()


def _dispatch_alert_nothing():
    _trip_in(timedelta(days=5), "Sam Ortiz", covered=True)
    monitoring.run_dispatch_monitor()


REPORTS = {
    "deposit_report_two_orders": _deposit_report_two_orders,
    "deposit_report_one_order": _deposit_report_one_order,
    "deposit_report_empty": _deposit_report_empty,
    "unconfirmed_two_customers": _unconfirmed_two_customers,
    "unconfirmed_one_trip": _unconfirmed_one_trip,
    "unconfirmed_empty": _unconfirmed_empty,
    "review_report": _review_report,
    "review_report_no_ratings": _review_report_no_ratings,
    "review_report_empty": _review_report_empty,
    "complaint_report_two": _complaint_report_two,
    "complaint_report_one": _complaint_report_one,
    "complaint_report_empty": _complaint_report_empty,
    "task_digest_two_departments": _task_digest_two_departments,
    "task_digest_one_task": _task_digest_one_task,
    "task_digest_nothing_overdue": _task_digest_nothing_overdue,
    "dispatch_alert_critical": _dispatch_alert_critical,
    "dispatch_alert_warning_only": _dispatch_alert_warning_only,
    "dispatch_alert_nothing": _dispatch_alert_nothing,
}
# The scenarios where nothing is due: they record that nothing went out.
EMPTY = {name for name in REPORTS if name.endswith(("_empty", "_nothing", "_nothing_overdue"))}


# --- capture ---------------------------------------------------------------------------


def _text(value: str) -> str:
    """Whitespace-normalised, with what differs between runs (pks, quote numbers) named."""
    return " ".join(_NUMBER.sub(r"\1<PK>", s.normalise(value)).split())


def _outcome(name: str) -> list[dict]:
    """Run the report once at the scenario's moment; every send it made, normalised and
    sorted."""
    calls: list[dict] = []

    def send(*, identifier, body, channel_type="phone", **_):
        calls.append({"channel_type": channel_type, "identifier": identifier, "body": body})
        return {"uid": f"golden-{len(calls)}"}

    with s.frozen(), patch(SEND, side_effect=send):
        mail.outbox.clear()
        REPORTS[name]()
        for message in mail.outbox:
            calls.append(
                {"email_to": list(message.to), "subject": message.subject, "body": message.body}
            )
        normalised = [
            {k: _text(v) if isinstance(v, str) else v for k, v in c.items()} for c in calls
        ]
    return sorted(normalised, key=lambda c: json.dumps(c, sort_keys=True))


def _expected() -> dict:
    return json.loads(EXPECTED.read_text()) if EXPECTED.exists() else {}


@pytest.mark.parametrize("name", sorted(REPORTS))
def test_reports_golden(name):
    got = _outcome(name)
    recorded = _expected()
    if RECORD and name not in recorded:
        # Recording only ever adds a new scenario; an existing entry is never rewritten.
        recorded[name] = got
        EXPECTED.write_text(
            json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return
    assert got == recorded[name]


@pytest.mark.skipif(RECORD, reason="recording")
def test_every_report_is_recorded():
    assert set(_expected()) == set(REPORTS)


@pytest.mark.skipif(RECORD, reason="recording")
def test_the_recording_is_real_copy():
    """Every report with something in it emailed its list; a quiet one sent nothing; no
    blank body, no "None", no unrendered placeholder."""
    for name, calls in _expected().items():
        if name in EMPTY:
            assert calls == [], name
            continue
        assert any("email_to" in call for call in calls), name
        for call in calls:
            text = " ".join(str(v) for v in call.values())
            assert "None" not in text, name
            assert not re.search(r"\{\w+\}", text), name
            assert call["body"].strip(), name
