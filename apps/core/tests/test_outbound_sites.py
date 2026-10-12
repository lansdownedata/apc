"""The go-live switch at every place the portal sends something (APC-90).

One test per send site, all with the switch off. Two halves:

* mail and texts to our own staff still go — they are how the office watches the portal
  run before it is live;
* everything aimed at a customer or an affiliate does not. Where a person pressed the
  button, they are told nothing went and nothing else changes; where the system sent on
  its own, it is quietly held back and logged.

"Still goes" and "does not" are both measured at the transport — Django's outbox and
Podium's HTTP call — never at a patched send function, which would walk around the gate.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services as account_services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.core import outbound
from apps.dispatch import monitoring
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment, DispatchAlertConfig
from apps.integrations import podium
from apps.integrations.factories import PodiumCredentialFactory
from apps.leads import services as lead_services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.factories import ConversationFactory, TouchPointFactory
from apps.messaging.models import Message, TouchPoint
from apps.notifications.models import Notification
from apps.payments import services as payment_services
from apps.payments.factories import PaymentPlanFactory
from apps.payments.tasks import send_unpaid_deposit_report
from apps.public.services import send_wedding_confirmation
from apps.reservations import tasks as reservation_tasks
from apps.reservations.factories import ReservationFactory, TransferReservationFactory
from apps.tasks import escalation
from apps.tasks import services as task_services
from apps.tasks.jobs import run_tasks
from apps.tasks.models import Task, TaskConfig
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def off(settings):
    settings.OUTBOUND_ENABLED = False


@pytest.fixture
def podium_http():
    """Podium's HTTP transport. A call here is a message that really left."""
    PodiumCredentialFactory(access_token="AT", expires_at=timezone.now() + timedelta(hours=5))
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"uid": "msg-1"}
    with patch.object(podium.requests, "request", return_value=fake) as request:
        yield request


@pytest.fixture
def agent(client):
    user = UserFactory(role=User.Role.OWNER_ADMIN)
    client.force_login(user)
    return user


def _booked_trip(**kw):
    return ReservationFactory(lead=LeadFactory(status=Lead.Status.BOOKED), **kw)


# === our own staff: still sent =======================================================


def test_the_unpaid_deposit_report_still_reaches_the_office(settings):
    settings.DEPOSIT_REPORT_EMAILS = ["office@allprocharter.com"]
    today = date(2026, 9, 1)
    lead = LeadFactory(status=Lead.Status.BOOKED)
    TransferReservationFactory(
        lead=lead, rate=Decimal("400.00"), pickup_date=today + timedelta(days=5)
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("400.00"))

    send_unpaid_deposit_report(today=today)

    assert [m.to for m in mail.outbox] == [["office@allprocharter.com"]]


def test_the_unconfirmed_trips_report_still_reaches_the_office(settings):
    settings.TRIP_CONFIRM_REPORT_EMAILS = ["office@allprocharter.com"]
    _booked_trip(pickup_date=timezone.localdate() + timedelta(days=1))

    reservation_tasks.send_unconfirmed_trip_report()

    assert [m.to for m in mail.outbox] == [["office@allprocharter.com"]]


def test_the_overdue_task_digest_still_reaches_the_office():
    cfg = TaskConfig.load()
    cfg.digest_emails = "ops@allprocharter.com"
    cfg.save()
    UserFactory(role=User.Role.OWNER_ADMIN)
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=10),
        pickup_time=time(9, 0),
        pickup_timezone="America/New_York",
    )
    task_services.ensure_tasks(lead)
    morning = datetime.combine(
        timezone.localdate(),
        time(escalation.DIGEST_HOUR, 5),
        tzinfo=timezone.get_current_timezone(),
    )
    Task.objects.filter(lead=lead).update(status=Task.Status.NOT_APPLICABLE)
    Task.objects.filter(lead=lead, kind="final_itinerary").update(
        status=Task.Status.OPEN,
        opens_at=morning - timedelta(days=5),
        due_at=morning - timedelta(hours=3),
    )

    with patch("django.utils.timezone.now", return_value=morning):
        run_tasks()

    digests = [m for m in mail.outbox if "overdue" in m.subject.lower()]
    assert [m.to for m in digests] == [["ops@allprocharter.com"]]


def _uncovered_trip_three_hours_out():
    when = timezone.localtime() + timedelta(hours=3)
    return _booked_trip(pickup_date=when.date(), pickup_time=when.time().replace(microsecond=0))


def test_dispatch_exception_emails_still_reach_the_dispatchers():
    cfg = DispatchAlertConfig.load()
    cfg.alert_emails = "ops@allprocharter.com"
    cfg.save()
    _uncovered_trip_three_hours_out()

    monitoring.run_dispatch_monitor()

    assert [m.to for m in mail.outbox] == [["ops@allprocharter.com"]]


def test_critical_dispatch_texts_still_reach_the_dispatchers(podium_http):
    cfg = DispatchAlertConfig.load()
    cfg.critical_sms = "+15715550100"
    cfg.save()
    _uncovered_trip_three_hours_out()

    monitoring.run_dispatch_monitor()

    podium_http.assert_called_once()
    assert podium_http.call_args.kwargs["json"]["channel"]["identifier"] == "+15715550100"


def test_a_staff_invitation_still_reaches_the_new_hire(settings):
    settings.PUBLIC_BASE_URL = "https://apc.test"

    _, sent = account_services.invite_user(
        first_name="New",
        last_name="Hire",
        email="new.hire@allprocharter.com",
        role=User.Role.AGENT,
        can_manage_payments=False,
        actor=UserFactory(role=User.Role.OWNER_ADMIN),
    )

    assert sent is True
    assert [m.to for m in mail.outbox] == [["new.hire@allprocharter.com"]]


# === customers and affiliates: a person pressed the button ===========================


def _quotable_lead():
    lead = LeadFactory(
        status=Lead.Status.NEW,
        contact=ContactFactory(email="customer@example.com", phone="+13015550100"),
    )
    ReservationFactory(lead=lead, rate="500.00")
    return lead


def test_send_quote_is_refused_and_the_lead_is_left_alone(podium_http):
    lead = _quotable_lead()

    result = lead_services.send_quote(lead, base_url="https://x.test")

    assert result.ok is False
    assert result.http_status == 409
    assert result.error == outbound.OFF_MESSAGE
    lead.refresh_from_db()
    assert lead.status == Lead.Status.NEW
    assert lead.quote_sent_at is None
    assert not TouchPoint.objects.filter(lead=lead).exists()
    assert mail.outbox == []
    podium_http.assert_not_called()


def test_send_payment_link_is_refused_and_nothing_is_logged_as_sent(podium_http):
    lead = LeadFactory(
        status=Lead.Status.BOOKED, contact=ContactFactory(phone="+15715551212", email="")
    )
    PaymentPlanFactory(lead=lead, quote_total=Decimal("1000.00"), deposit_pct=50)

    with pytest.raises(payment_services.PaymentError, match="Outbound messaging is off"):
        payment_services.send_pay_link(lead, base_url="https://x.test", sender_name="Agent")

    podium_http.assert_not_called()
    assert not Message.objects.exists()


def test_the_inbox_composer_is_refused_and_no_message_is_recorded(client, agent, podium_http):
    convo = ConversationFactory(contact=ContactFactory(phone="+16175551234"))

    resp = client.post(
        reverse("inbox_send", args=[convo.pk]),
        data=json.dumps({"body": "On our way", "channel": "sms"}),
        content_type="application/json",
    )

    assert resp.status_code == 409
    assert resp.json() == {"ok": False, "error": outbound.OFF_MESSAGE}
    podium_http.assert_not_called()
    assert not Message.objects.filter(conversation=convo).exists()


def test_an_emailed_affiliate_offer_is_refused_before_the_trip_is_marked_offered():
    trip = _booked_trip(pickup_date=date(2026, 11, 2), pickup_time=time(6, 15), rate=285, hours=1)
    vendor = VendorFactory(name="Capital Chauffeurs", email="ops@capital.example")

    with pytest.raises(dispatch_services.AssignmentError, match="Outbound messaging is off"):
        dispatch_services.send_offer(trip, vendor, payout=Decimal("215.00"))

    assert not Assignment.objects.filter(reservation=trip).exists()
    assert not Notification.objects.filter(lead=trip.lead).exists()
    assert mail.outbox == []


def test_an_affiliate_cancellation_notice_is_refused_with_the_real_reason(client, agent):
    assignment = AssignmentFactory(
        reservation=_booked_trip(),
        status=Assignment.Status.CONFIRMED,
        vendor=VendorFactory(name="Reston Coach Co", email="ops@reston.example"),
    )

    resp = client.post(reverse("dispatch_cancel_notice", args=[assignment.pk]))

    assert resp.status_code == 409
    assert resp.json() == {"ok": False, "error": outbound.OFF_MESSAGE}
    assert mail.outbox == []


# === customers: the system sent on its own ===========================================


def test_the_wedding_confirmation_is_held_back():
    lead = LeadFactory(contact=ContactFactory(email="bride@example.com"))

    assert send_wedding_confirmation(lead, base_url="https://apc.test") is False

    assert mail.outbox == []


def test_a_due_touchpoint_is_skipped_with_the_reason_not_sent_or_left_to_pile_up(
    settings, podium_http
):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(contact=ContactFactory(email="a@example.com", phone="5551234567"))
    tp = TouchPointFactory(
        lead=lead,
        kind=TouchPoint.Kind.TP1_WELCOME,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )

    assert touchpoints.run_touchpoints() == 0

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    assert tp.error == touchpoints.OUTBOUND_OFF
    podium_http.assert_not_called()


def test_a_touchpoint_not_yet_due_stays_scheduled_for_after_go_live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    tp = TouchPointFactory(scheduled_for=timezone.now() + timedelta(days=2))

    touchpoints.run_touchpoints()

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED


def test_a_review_invitation_is_never_created(settings, podium_http):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(contact=ContactFactory(phone="5551234567"))
    tp = TouchPointFactory(
        lead=lead,
        kind=TouchPoint.Kind.REVIEW_REQUEST,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )

    touchpoints.run_touchpoints()

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    podium_http.assert_not_called()
