"""Built-in rules and send-time checks (spec C sections 3, 4 and 9).

Reason strings are the old engine's, pinned against the frozen golden recording where a
scenario covers them.
"""

import json
from datetime import time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.billing.factories import AccountGroupFactory
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import CustomerFeedback, Lead
from apps.messaging import rules
from apps.messaging.factories import MessageRowFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, NotificationConfig, TouchPoint
from apps.messaging.recipients import addresses, resolve
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations import reviews
from apps.reservations.acknowledgements import (
    affiliate_ack_url,
    trip_day_ack_url,
    wedding_day_ack_url,
    wedding_details_url,
)
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation, TripIssue
from apps.reservations.passengers import parse_passengers, save_passengers

pytestmark = pytest.mark.django_db

GOLDEN = json.loads((Path(__file__).parent / "golden" / "expected.json").read_text())
BASE = "https://apc.example.com"
SEND = ("send", "")


def golden(scenario: str) -> str:
    """The TouchPoint error today's engine writes for a recorded scenario."""
    return GOLDEN[scenario]["error"]


@pytest.fixture(autouse=True)
def _base_url(settings):
    settings.PUBLIC_BASE_URL = BASE


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _tp(row, lead, trip=None) -> TouchPoint:
    tp = TouchPointFactory(
        lead=lead, reservation=trip, kind=row.key, message=row, scheduled_for=timezone.now()
    )
    # As the sender loads it: fresh from the database.
    return TouchPoint.objects.select_related("lead", "reservation").get(pk=tp.pk)


def _booked(**contact) -> Lead:
    return LeadFactory(status=Lead.Status.BOOKED, contact=ContactFactory(**contact))


def _trip(lead, *, days: int = 5, at: time = time(14, 0), wedding=False, **kw) -> Reservation:
    return ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=days),
        pickup_time=at,
        pickup_timezone="America/New_York",
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None,
        **kw,
    )


def _status(trip, status) -> Reservation:
    Reservation.objects.filter(pk=trip.pk).update(trip_status=status)
    trip.refresh_from_db()
    return trip


def _dispatched(trip) -> Assignment:
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    dispatch_services.set_driver_info(
        a, name="Sam Rivera", cell="+15715551212", vehicle_desc="Suburban", vehicle_number="9"
    )
    _status(trip, Reservation.TripStatus.DISPATCHED)
    return a


def _to(tp, row):
    """Who the send would reach right now: resolved at send time, then the built-in override."""
    return rules.recipients_for(tp, row, resolve(row, tp.lead, tp.reservation))


# --- safety rules: every row ---------------------------------------------------------


def test_master_switch_off_skips():
    NotificationConfig.objects.update_or_create(singleton_id=1, defaults={"enabled": False})
    lead = LeadFactory()
    assert rules.check(_tp(_row("tp1_welcome"), lead), _row("tp1_welcome")) == (
        "skip",
        "message disabled",
    )


def test_master_switch_can_be_passed_in():
    lead = LeadFactory()
    row = _row("tp1_welcome")
    assert rules.check(_tp(row, lead), row, master=False) == ("skip", "message disabled")


def test_row_off_skips():
    row = _row("tp1_welcome")
    row.enabled = False
    row.save()
    assert rules.check(_tp(row, LeadFactory()), row) == ("skip", "message disabled")


def test_archived_row_skips():
    row = MessageRowFactory(archived_at=timezone.now(), enabled=False)
    assert rules.check(_tp(row, LeadFactory()), row) == ("skip", "message disabled")


def test_lost_order_skips():
    row = _row("tp6_quote_followup")
    lead = LeadFactory(status=Lead.Status.LOST)
    assert rules.check(_tp(row, lead), row) == ("skip", golden("quote_followup_skipped_lead_lost"))


def test_order_cancelled_trigger_still_sends_on_a_lost_order():
    row = MessageRowFactory(trigger="order_cancelled", sms_body="Sorry, {first_name}.")
    lead = LeadFactory(status=Lead.Status.LOST)
    assert rules.check(_tp(row, lead), row) == SEND


def test_cancelled_trip_skips():
    row = _row("trip_confirm_customer")
    lead = _booked()
    trip = _status(_trip(lead), Reservation.TripStatus.LATE_CANCEL)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "trip cancelled")


def test_trip_cancelled_trigger_still_sends_for_a_cancelled_trip():
    row = MessageRowFactory(trigger="trip_cancelled", sms_body="Cancelled.")
    lead = _booked()
    trip = _status(_trip(lead), Reservation.TripStatus.CANCELLED)
    assert rules.check(_tp(row, lead, trip), row) == SEND


def test_before_row_past_pickup_skips():
    row = _row("trip_confirm_customer")
    lead = _booked()
    trip = _trip(lead, days=-1)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "pickup passed")


def test_after_row_past_pickup_still_sends():
    row = MessageRowFactory(
        trigger="pickup", timing_direction="after", timing_amount=2, sms_body="Thanks!"
    )
    lead = _booked()
    trip = _trip(lead, days=-1)
    assert rules.check(_tp(row, lead, trip), row) == SEND


def test_waits_for_dispatched_with_driver():
    row = _row("driver_released")
    lead = _booked()
    trip = _trip(lead, days=2)
    tp = _tp(row, lead, trip)
    assert rules.check(tp, row)[0] == "wait"
    _dispatched(trip)
    assert rules.check(_tp(row, lead, trip), row) == SEND


def test_waiting_row_held_on_a_cancelled_trip_not_skipped():
    """Today's driver message: held while cancelled, so a reinstated trip still gets it."""
    row = _row("driver_released")
    lead = _booked()
    trip = _trip(lead, days=2)
    _dispatched(trip)
    _status(trip, Reservation.TripStatus.CANCELLED)
    assert rules.check(_tp(row, lead, trip), row)[0] == "wait"


def test_waiting_row_skips_once_pickup_passes():
    row = _row("driver_released")
    lead = _booked()
    trip = _trip(lead, days=-1)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "pickup passed")


def test_driver_updated_waits_like_the_driver_message():
    row = _row("driver_updated")
    lead = _booked()
    trip = _trip(lead, days=2)
    assert rules.check(_tp(row, lead, trip), row)[0] == "wait"
    _dispatched(trip)
    assert rules.check(_tp(row, lead, trip), row) == SEND
    past = _trip(lead, days=-1)
    assert rules.check(_tp(row, lead, past), row) == ("skip", "pickup passed")


def test_link_without_public_base_url_waits(settings):
    settings.PUBLIC_BASE_URL = ""
    row = _row("tp6_quote_followup")
    assert rules.check(_tp(row, LeadFactory()), row)[0] == "wait"


def test_no_link_sends_without_public_base_url(settings):
    settings.PUBLIC_BASE_URL = ""
    row = MessageRowFactory(sms_body="Hi {first_name}", email_body="", subject="")
    assert rules.check(_tp(row, LeadFactory()), row) == SEND


def test_link_only_in_a_channel_the_row_does_not_send_is_ignored(settings):
    settings.PUBLIC_BASE_URL = ""
    row = MessageRowFactory(send_email=False, sms_body="Hi", email_body="Book: {quote_link}")
    assert rules.check(_tp(row, LeadFactory()), row) == SEND


def test_trip_service_type_no_longer_matches():
    row = _row("wed_final_details")
    lead = _booked()
    trip = _trip(lead, days=10, wedding=True)
    tp = _tp(row, lead, trip)
    assert rules.check(tp, row) == SEND
    trip.service_type = ServiceTypeFactory(name="Airport Transfer")
    trip.save()
    tp = TouchPoint.objects.get(pk=tp.pk)
    assert rules.check(tp, row) == ("skip", "service type no longer matches")


def test_order_service_type_matches_any_live_trip():
    hourly = ServiceTypeFactory(name="Hourly Charter")
    row = MessageRowFactory(trigger="all_trips_done", sms_body="Thanks")
    row.service_types.set([hourly])
    lead = _booked()
    charter = _trip(lead)
    charter.service_type = hourly
    charter.save()
    _trip(lead)
    assert rules.check(_tp(row, lead), row) == SEND
    _status(charter, Reservation.TripStatus.CANCELLED)
    assert rules.check(_tp(row, lead), row) == ("skip", "service type no longer matches")


def test_trip_level_needs_a_booked_order():
    row = _row("trip_confirm_customer")
    lead = LeadFactory(status=Lead.Status.QUOTED)
    trip = _trip(lead)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "lead not booked")


def test_trip_level_without_a_trip_skips():
    row = _row("trip_confirm_customer")
    assert rules.check(_tp(row, _booked()), row) == ("skip", "no reservation")


def test_trip_date_row_without_a_pickup_date_skips():
    row = _row("trip_confirm_customer")
    lead = _booked()
    trip = ReservationFactory(lead=lead, pickup_date=None)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "trip has no pickup date")


# --- tp1-tp8: the quote program -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "scenario"),
    [
        (Lead.Status.BOOKED, "quote_followup_skipped_lead_booked"),
        (Lead.Status.ENGAGED, "quote_followup_skipped_lead_engaged"),
    ],
)
def test_quote_program_stops_once_booked_or_engaged(status, scenario):
    lead = LeadFactory(status=status)
    for key in ("tp1_welcome", "tp2_lead_followup", "tp6_quote_followup", "tp8_expired"):
        row = _row(key)
        assert rules.check(_tp(row, lead), row) == ("skip", golden(scenario)), key


@pytest.mark.parametrize(
    "deposit", [PaymentPlan.DepositStatus.PAID, PaymentPlan.DepositStatus.AUTHORIZED]
)
def test_quote_messages_stop_once_the_deposit_is_taken(deposit):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    PaymentPlanFactory(lead=lead, deposit_status=deposit)
    for key in ("tp3_quote_sent_sms", "tp6_quote_followup", "tp7_expiring"):
        row = _row(key)
        assert rules.check(_tp(row, lead), row) == (
            "skip",
            golden("quote_followup_skipped_deposit_taken"),
        ), key
    welcome = _row("tp1_welcome")
    assert rules.check(_tp(welcome, lead), welcome) == SEND


def test_quote_program_sends_while_quoted():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    row = _row("tp6_quote_followup")
    assert rules.check(_tp(row, lead), row) == SEND


def test_added_row_on_a_quote_trigger_has_no_quote_program_rules():
    row = MessageRowFactory(trigger="quote_sent", sms_body="Hi")
    lead = LeadFactory(status=Lead.Status.BOOKED)
    assert rules.check(_tp(row, lead), row) == SEND


# --- payment_reminder -----------------------------------------------------------------


def _reminder_plan(lead=None, **kw):
    kw.setdefault("quote_total", Decimal("1000.00"))
    kw.setdefault("stripe_payment_method_id", "pm_1")
    kw.setdefault("card_last4", "4242")
    kw.setdefault("balance_status", PaymentPlan.BalanceStatus.SCHEDULED)
    return PaymentPlanFactory(lead=lead or _booked(), **kw)


def test_payment_reminder_sends():
    plan = _reminder_plan()
    row = _row("payment_reminder")
    assert rules.check(_tp(row, plan.lead), row) == SEND


def test_payment_reminder_skips_an_account_order():
    plan = _reminder_plan()
    Lead.objects.filter(pk=plan.lead_id).update(billing_group=AccountGroupFactory())
    row = _row("payment_reminder")
    assert rules.check(_tp(row, plan.lead), row) == (
        "skip",
        golden("payment_reminder_account_order"),
    )


@pytest.mark.parametrize(
    ("plan_fields", "reason"),
    [
        ({"stripe_payment_method_id": "", "card_last4": ""}, "no card on file"),
        ({"balance_status": PaymentPlan.BalanceStatus.NA}, "balance not scheduled"),
        ({"quote_total": Decimal("0.00")}, "nothing owed"),
    ],
)
def test_payment_reminder_skip_reasons(plan_fields, reason):
    plan = _reminder_plan(**plan_fields)
    row = _row("payment_reminder")
    assert rules.check(_tp(row, plan.lead), row) == ("skip", reason)


def test_payment_reminder_without_a_plan_skips():
    row = _row("payment_reminder")
    assert rules.check(_tp(row, _booked()), row) == ("skip", "no card on file")


def test_a_seeded_row_moved_to_another_trigger_loses_its_built_in_rules():
    plan = _reminder_plan()
    Lead.objects.filter(pk=plan.lead_id).update(billing_group=AccountGroupFactory())
    row = _row("payment_reminder")
    row.trigger = "balance_paid"
    row.save()
    assert not rules.has_built_ins(row)
    assert rules.check(_tp(row, plan.lead), row) == SEND


# --- trip_confirm_customer(_2) --------------------------------------------------------


def test_plain_confirmation_goes_to_the_booker_keyed_on_the_local_day():
    row = _row("trip_confirm_customer")
    lead = _booked()
    trip = _trip(lead, at=time(23, 30))  # already the next day in UTC
    tp = _tp(row, lead, trip)
    assert [r.contact_id for r in _to(tp, row)] == [lead.contact_id]
    assert rules.combine_key(tp, row) == trip.pickup_date.isoformat()
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)
    assert ctx["confirm_link"] == trip_day_ack_url(lead.contact, trip.pickup_date, base_url=BASE)


@pytest.mark.parametrize("key", ["trip_confirm_customer", "trip_confirm_customer_2"])
def test_wedding_confirmation_goes_to_the_billing_contact_only(key):
    row = _row(key)
    payer = ContactFactory(name="Pat Okafor", phone="+12025550103")
    lead = _booked(name="Casey Morgan")
    lead.billing_contact = payer
    lead.save()
    trip = _trip(lead, wedding=True)
    tp = _tp(row, lead, trip)
    [to] = _to(tp, row)
    assert (to.contact_id, to.label) == (payer.pk, "Pat Okafor (billing contact)")
    assert rules.combine_key(tp, row) == f"{trip.pickup_date.isoformat()}:wedding:{lead.pk}"
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)
    assert ctx["confirm_link"] == wedding_day_ack_url(lead, trip.pickup_date, base_url=BASE)


def test_farmed_in_wedding_confirmation_goes_to_the_operator_only():
    operator = CompanyFactory(
        is_affiliate_account=True, billing_contact=ContactFactory(name="Avery Park")
    )
    lead = _booked(name="Riley Chen", company=operator)
    trip = _trip(lead, wedding=True)
    row = _row("trip_confirm_customer")
    [to] = _to(_tp(row, lead, trip), row)
    assert (to.contact_id, to.label) == (lead.contact_id, "Riley Chen (operator)")


def test_plain_day_already_confirmed_skips():
    row = _row("trip_confirm_customer_2")
    lead = _booked()
    trip = _trip(lead)
    tp = _tp(row, lead, trip)
    assert rules.check(tp, row) == SEND
    Reservation.objects.filter(pk=trip.pk).update(customer_confirmed_at=timezone.now())
    tp = TouchPoint.objects.get(pk=tp.pk)
    assert rules.check(tp, row) == ("skip", golden("trip_confirm_customer_2_day_confirmed"))


def test_plain_day_with_an_unconfirmed_sibling_still_sends():
    row = _row("trip_confirm_customer_2")
    lead = _booked()
    trip = _trip(lead, customer_confirmed_at=timezone.now())
    other = LeadFactory(status=Lead.Status.BOOKED, contact=lead.contact)
    _trip(other, at=time(18, 0))
    assert rules.check(_tp(row, lead, trip), row) == SEND


def test_wedding_day_confirmed_but_fees_owed_still_sends():
    row = _row("trip_confirm_customer_2")
    lead = _booked()
    trip = _trip(lead, wedding=True, customer_confirmed_at=timezone.now())
    assert rules.check(_tp(row, lead, trip), row) == SEND
    Lead.objects.filter(pk=lead.pk).update(fees_acknowledged_at=timezone.now())
    assert rules.check(_tp(row, lead, trip), row) == (
        "skip",
        golden("wedding_confirm_48h_confirmed_fees_acknowledged"),
    )


def test_wedding_confirmed_day_ignores_the_bookers_plain_trips():
    """A plain trip's confirmation leaves the wedding out of its group, and the reverse."""
    row = _row("trip_confirm_customer_2")
    lead = _booked()
    wedding = _trip(lead, wedding=True)
    plain_order = LeadFactory(status=Lead.Status.BOOKED, contact=lead.contact)
    plain = _trip(plain_order, customer_confirmed_at=timezone.now())
    assert rules.check(_tp(row, plain_order, plain), row)[1] == "day already confirmed"
    Lead.objects.filter(pk=lead.pk).update(fees_acknowledged_at=timezone.now())
    assert rules.check(_tp(row, lead, wedding), row) == SEND


def test_covered_reason_is_todays_string():
    """Combining belongs to the sender (Task 9); it writes this when every person already
    had the day's grouped notice."""
    assert rules.COVERED == golden("two_trips_same_day_second_stands_down")


def test_moved_confirmation_loses_the_wedding_override():
    row = _row("trip_confirm_customer")
    row.trigger = "affiliate_accepted"
    row.save()
    payer = ContactFactory()
    lead = _booked()
    lead.billing_contact = payer
    lead.save()
    trip = _trip(lead, wedding=True)
    tp = _tp(row, lead, trip)
    assert [r.contact_id for r in _to(tp, row)] == [lead.contact_id]
    assert rules.combine_key(tp, row) == ""


# --- thank_you / thank_you_wedding ----------------------------------------------------


def test_thank_you_sends_on_a_plain_order():
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1)
    assert rules.check(_tp(row, lead), row) == SEND


@pytest.mark.parametrize("by", ["wedding_trip", "wedding_name"])
def test_thank_you_stands_down_for_the_wedding_thank_you(by):
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1, wedding=by == "wedding_trip")
    if by == "wedding_name":
        Lead.objects.filter(pk=lead.pk).update(wedding_name="Morgan & Okafor Wedding")
    _tp(_row("thank_you_wedding"), lead)
    assert rules.check(_tp(row, lead), row) == ("skip", "wedding thank-you sends instead")


def test_thank_you_sends_for_a_wedding_when_the_wedding_thank_you_is_off():
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1, wedding=True)
    _tp(_row("thank_you_wedding"), lead)
    MessageTemplate.objects.filter(key="thank_you_wedding").update(enabled=False)
    assert rules.check(_tp(row, lead), row) == SEND


def test_thank_you_sends_when_no_wedding_thank_you_was_queued():
    """The wedding row is on, but nothing was queued for this lead (queued before the
    cut-over, or the row was switched on after all trips were done)."""
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1, wedding=True)
    assert rules.check(_tp(row, lead), row) == SEND


@pytest.mark.parametrize("status", [TouchPoint.Status.SKIPPED, TouchPoint.Status.CANCELLED])
def test_thank_you_sends_when_the_wedding_thank_you_stood_down(status):
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1, wedding=True)
    TouchPoint.objects.filter(pk=_tp(_row("thank_you_wedding"), lead).pk).update(status=status)
    assert rules.check(_tp(row, lead), row) == SEND


def test_thank_you_stands_down_once_the_wedding_thank_you_went_out():
    """Sent is final: switching the wedding row off afterwards doesn't send a second one."""
    row = _row("thank_you")
    lead = _booked()
    _trip(lead, days=-1, wedding=True)
    sent = _tp(_row("thank_you_wedding"), lead)
    TouchPoint.objects.filter(pk=sent.pk).update(status=TouchPoint.Status.SENT)
    MessageTemplate.objects.filter(key="thank_you_wedding").update(enabled=False)
    assert rules.check(_tp(row, lead), row) == ("skip", "wedding thank-you sends instead")


def test_thank_you_needs_a_booked_order():
    lead = LeadFactory(status=Lead.Status.QUOTED)
    _trip(lead, days=-1, wedding=True)
    for key in ("thank_you", "thank_you_wedding"):
        row = _row(key)
        assert rules.check(_tp(row, lead), row) == ("skip", "lead not booked"), key


def test_wedding_thank_you_sends_for_a_wedding_name_without_a_wedding_trip():
    row = _row("thank_you_wedding")
    lead = _booked()
    _trip(lead, days=-1)
    assert rules.check(_tp(row, lead), row) == ("skip", "service type no longer matches")
    Lead.objects.filter(pk=lead.pk).update(wedding_name="Morgan & Okafor Wedding")
    assert rules.matches(row, Lead.objects.get(pk=lead.pk))
    assert rules.check(_tp(row, lead), row) == SEND


def test_thank_you_links_to_the_last_days_trip_page():
    lead = _booked()
    _trip(lead, days=-3)
    last = _trip(lead, days=-1)
    for key in ("thank_you", "thank_you_wedding"):
        row = _row(key)
        ctx = {}
        rules.fill_links(_tp(row, lead), row, ctx, BASE)
        assert ctx["confirm_link"] == trip_day_ack_url(
            lead.contact, last.pickup_date, base_url=BASE
        )


# --- review_request -------------------------------------------------------------------


def _finished_trip():
    lead = _booked(phone="+12025550101")
    trip = _status(_trip(lead, days=-1), Reservation.TripStatus.DONE)
    return lead, trip


def test_review_request_held_by_a_complaint():
    lead, trip = _finished_trip()
    reviews.add_issue(
        reviews.review_for(trip),
        category=TripIssue.Category.COMPLAINT,
        severity=TripIssue.Severity.HIGH,
        note="Late.",
        user=UserFactory(),
    )
    row = _row("review_request")
    with patch("apps.tasks.services.sync") as sync:
        result = rules.check(_tp(row, lead), row)
    assert result == ("cancel", golden("review_request_held_by_complaint"))
    sync.assert_called_once_with(lead)


def test_review_request_held_by_a_low_rating():
    lead, _ = _finished_trip()
    CustomerFeedback.objects.create(lead=lead, rating=2, submitted_at=timezone.now())
    row = _row("review_request")
    with patch("apps.tasks.services.sync"):
        result = rules.check(_tp(row, lead), row)
    assert result == ("cancel", golden("review_request_held_by_low_rating"))


def test_review_request_without_a_phone_skips():
    lead, _ = _finished_trip()
    lead.contact.phone = ""
    lead.contact.save()
    row = _row("review_request")
    assert rules.check(_tp(row, lead), row) == ("skip", golden("review_request_no_phone"))


def test_review_request_sends():
    lead, _ = _finished_trip()
    row = _row("review_request")
    assert rules.check(_tp(row, lead), row) == SEND


# --- wed_final_details / trip_confirm_affiliate ---------------------------------------


def test_wedding_details_request_links_to_the_details_form_per_trip():
    row = _row("wed_final_details")
    lead = _booked()
    trip = _trip(lead, days=10, wedding=True)
    tp = _tp(row, lead, trip)
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)
    assert ctx["confirm_link"] == wedding_details_url(trip, base_url=BASE)
    assert rules.combine_key(tp, row) == f"{trip.pickup_date.isoformat()}:trip:{trip.pk}"


def test_affiliate_confirmation_links_to_the_ack_page_per_trip():
    row = _row("trip_confirm_affiliate")
    lead = _booked()
    trip = _trip(lead)
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    tp = _tp(row, lead, trip)
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)
    assert ctx["ack_link"] == affiliate_ack_url(a, base_url=BASE)
    assert "confirm_link" not in ctx
    assert rules.combine_key(tp, row) == f"{trip.pickup_date.isoformat()}:trip:{trip.pk}"


def test_affiliate_confirmation_skips_without_a_confirmed_affiliate():
    """Today's rule: the row is only queued once a manual vendor confirms (re-confirming
    queues a fresh one), so coverage gone by send time closes it."""
    row = _row("trip_confirm_affiliate")
    lead = _booked()
    trip = _trip(lead)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "no confirmed affiliate")
    AssignmentFactory(reservation=trip, status=Assignment.Status.OFFERED)
    assert rules.check(_tp(row, lead, trip), row) == ("skip", "no confirmed affiliate")


def test_affiliate_confirmation_sends_for_a_manual_farm_out():
    row = _row("trip_confirm_affiliate")
    lead = _booked()
    trip = _trip(lead)
    AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    assert rules.check(_tp(row, lead, trip), row) == SEND


def test_affiliate_confirmation_skips_gnet_coverage():
    """GNet's acceptance is its acknowledgement (golden gnet_assignment_no_affiliate_*)."""
    row = _row("trip_confirm_affiliate")
    lead = _booked()
    trip = _trip(lead)
    AssignmentFactory(
        reservation=trip, status=Assignment.Status.CONFIRMED, channel=Assignment.Channel.GNET
    )
    assert rules.check(_tp(row, lead, trip), row) == (
        "skip",
        "no affiliate confirmation for GNet coverage",
    )


def test_affiliate_confirmation_skips_in_house_coverage():
    row = _row("trip_confirm_affiliate")
    lead = _booked()
    trip = _trip(lead)
    AssignmentFactory(reservation=trip, in_house=True)
    assert rules.check(_tp(row, lead, trip), row) == (
        "skip",
        "no affiliate confirmation for in-house coverage",
    )


def test_unknown_wait_is_never_ready():
    trip = _dispatched_trip()
    assert rules.ready(trip, "dispatched_with_driver")
    assert not rules.ready(trip, "something_new")


def test_cancelled_trip_is_never_ready():
    trip = _dispatched_trip()
    _status(trip, Reservation.TripStatus.CANCELLED)
    assert not rules.ready(trip, "dispatched_with_driver")


def _dispatched_trip() -> Reservation:
    trip = _trip(_booked(), days=2)
    _dispatched(trip)
    return trip


def test_driver_message_is_per_trip():
    row = _row("driver_released")
    lead = _booked()
    trip = _trip(lead)
    assert rules.combine_key(_tp(row, lead, trip), row).endswith(f":trip:{trip.pk}")


# --- added rows: generic links, no combining off a combining trigger ------------------


def test_added_trip_row_gets_generic_links():
    row = MessageRowFactory(trigger="status_arrived", sms_body="{confirm_link}")
    lead = _booked()
    trip = _trip(lead)
    tp = _tp(row, lead, trip)
    ctx = {}
    rules.fill_links(tp, row, ctx, BASE)
    assert ctx["confirm_link"] == trip_day_ack_url(lead.contact, trip.pickup_date, base_url=BASE)
    assert ctx["quote_link"].startswith(BASE) and ctx["pay_link"].startswith(BASE)
    assert rules.combine_key(tp, row) == ""


def test_added_order_row_gets_quote_and_pay_links_only():
    row = MessageRowFactory(trigger="deposit_paid", sms_body="{pay_link}")
    lead = _booked()
    ctx = {}
    rules.fill_links(_tp(row, lead), row, ctx, BASE)
    assert set(ctx) == {"quote_link", "pay_link"}


def test_added_pickup_row_combines_on_the_local_day():
    row = MessageRowFactory(trigger="pickup", timing_direction="before", sms_body="Soon")
    lead = _booked()
    trip = _trip(lead, at=time(23, 30))
    assert rules.combine_key(_tp(row, lead, trip), row) == trip.pickup_date.isoformat()


# --- editor text ----------------------------------------------------------------------


def test_built_in_descriptions():
    assert rules.built_in_descriptions("payment_reminder")
    assert rules.built_in_descriptions("trip_confirm_customer_2")
    assert rules.built_in_descriptions("thank_you")
    assert rules.built_in_descriptions("review_request")
    assert rules.built_in_descriptions("order_confirmed") == []
    assert rules.built_in_descriptions("custom-7") == []


# --- Review Focus 5 -------------------------------------------------------------------


def test_resolution_happens_at_send_time():
    lead = _booked(name="Bea Booker", phone="+12025550100")
    trip = _trip(lead, days=2)
    _dispatched(trip)
    row = _row("driver_released")
    row.recipients = ["passengers"]
    row.save()
    save_passengers(
        trip,
        parse_passengers(
            [
                {"name": "Lee Lead", "mobile": "+12025550131", "lead": True},
                {"name": "Ash Rider", "mobile": "+12025550132"},
                {"name": "Kit Quiet", "mobile": ""},
            ]
        ),
    )
    tp = _tp(row, lead, trip)
    assert [a for _, a in addresses(_to(tp, row), "sms")] == ["+12025550131"]

    # The lead passenger's mobile is cleared after queueing.
    save_passengers(
        trip,
        parse_passengers(
            [
                {"name": "Lee Lead", "mobile": "", "lead": True},
                {"name": "Ash Rider", "mobile": "+12025550132"},
                {"name": "Kit Quiet", "mobile": ""},
            ]
        ),
    )
    tp = TouchPoint.objects.select_related("lead", "reservation").get(pk=tp.pk)
    assert rules.check(tp, row) == SEND
    sent_to = addresses(_to(tp, row), "sms")
    assert [(r.label, a) for r, a in sent_to] == [("Ash Rider (passenger)", "+12025550132")]

    # The booker's company becomes an affiliate account: a farmed-in order.
    lead.contact.company = CompanyFactory(is_affiliate_account=True)
    lead.contact.save()
    tp = TouchPoint.objects.select_related("lead", "reservation").get(pk=tp.pk)
    assert rules.check(tp, row) == SEND
    [to] = _to(tp, row)
    assert (to.contact_id, to.label) == (lead.contact_id, "Bea Booker (operator)")


def test_golden_reasons_are_unchanged():
    """The reasons this module writes, against the frozen recording."""
    assert {
        golden(s)
        for s in (
            "payment_reminder_account_order",
            "quote_followup_skipped_deposit_taken",
            "quote_followup_skipped_lead_booked",
            "quote_followup_skipped_lead_engaged",
            "quote_followup_skipped_lead_lost",
            "review_request_held_by_complaint",
            "review_request_held_by_low_rating",
            "review_request_no_phone",
            "trip_confirm_customer_2_day_confirmed",
            "wedding_confirm_48h_confirmed_fees_acknowledged",
            "two_trips_same_day_second_stands_down",
        )
    } == {
        "billed to an account",
        "deposit already taken",
        "lead BOOKED",
        "lead ENGAGED",
        "lead LOST",
        "Review request held: complaint logged",
        "Review request held: low rating",
        "no phone for review invite",
        "day already confirmed",
        "covered by the grouped notice",
    }
