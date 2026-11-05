"""The shared sender (spec C sections 5 and 9): deliveries, combining, transport, the cron.

Podium and Postmark are always mocked. Seeded rows come from the seed migration; added rows
from `MessageRowFactory`.
"""

from datetime import date, time, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact
from apps.core import cron
from apps.dispatch import services as dispatch_services
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.integrations.podium import PodiumAPIError
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging import rules, sender
from apps.messaging.factories import DeliveryFactory, MessageRowFactory, TouchPointFactory
from apps.messaging.models import Delivery, MessageTemplate, Review, TouchPoint
from apps.messaging.recipients import Recipient
from apps.planners.models import PlannerAccount
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations.factories import PassengerFactory, ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

SEND = "apps.integrations.podium.send_message"
INVITE = "apps.integrations.podium.create_review_invitation"
POSTMARK = "apps.notifications.email.deliver_html_email"
BASE = "https://apc.example.com"
NOV_14 = date(timezone.localdate().year + 1, 11, 14)


@pytest.fixture(autouse=True)
def live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = BASE


def _ok(*_, **__):
    _ok.n = getattr(_ok, "n", 0) + 1
    return {"uid": f"uid-{_ok.n}"}


def _row(key: str) -> MessageTemplate:
    return MessageTemplate.objects.get(key=key)


def _booked(name="Bea Booker", phone="+12025550100", email="bea.booker@example.com", **kw):
    return LeadFactory(
        status=Lead.Status.BOOKED,
        contact=ContactFactory(name=name, phone=phone, email=email),
        **kw,
    )


def _trip(lead, day=NOV_14, at=time(14, 0), *, wedding=False, **kw) -> Reservation:
    return ReservationFactory(
        lead=lead,
        pickup_date=day,
        pickup_time=at,
        pickup_timezone="America/New_York",
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME) if wedding else None,
        **kw,
    )


def _tp(row, lead, trip=None, *, legacy=False, due=True) -> TouchPoint:
    when = timezone.now() - timedelta(minutes=1) if due else timezone.now() + timedelta(days=1)
    tp = TouchPointFactory(
        lead=lead,
        reservation=trip,
        kind=row.key,
        message=None if legacy else row,
        scheduled_for=when,
    )
    return TouchPoint.objects.select_related("lead", "reservation", "message").get(pk=tp.pk)


def _added(**kw) -> MessageTemplate:
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("subject", "Hello {first_name}")
    kw.setdefault("email_body", "Email for {first_name}")
    kw.setdefault("sms_body", "Text for {first_name}")
    return MessageRowFactory(**kw)


def _pickup_row(**kw) -> MessageTemplate:
    """An added message on the combining pickup trigger, 72 hours before."""
    return _added(
        trigger="pickup",
        section=MessageTemplate.Section.TRIP_DAY,
        timing_amount=72,
        timing_unit="hours",
        timing_direction=MessageTemplate.Direction.BEFORE,
        sms_body="{first_name}, your trips:\n{trip_sheet}",
        email_body="{trip_sheet}",
        **kw,
    )


def _calls(send) -> list[tuple[str, str]]:
    return sorted((c.kwargs["channel_type"], c.kwargs["identifier"]) for c in send.call_args_list)


def _deliveries(tp) -> list[tuple]:
    return sorted(
        (d.channel, d.address, d.recipient_label, d.status, d.reason)
        for d in Delivery.objects.filter(touchpoint=tp)
    )


# --- deliveries ----------------------------------------------------------------------


def test_one_delivery_per_recipient_and_channel_with_labels():
    lead = _booked()
    couple = ContactFactory(name="Cal Couple", phone="(202) 555-0111", email="cal@example.com")
    LeadContact.objects.create(lead=lead, contact=couple, role=LeadContact.Role.COUPLE)
    row = _added(recipients=["booking_contact", "role:couple"])
    tp = _tp(row, lead)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True

    # Phones go out in E.164, emails as stored (a staff address keeps its capitals, below).
    assert _calls(send) == [
        ("email", "bea.booker@example.com"),
        ("email", "cal@example.com"),
        ("phone", "+12025550100"),
        ("phone", "+12025550111"),
    ]
    sent = Delivery.Status.SENT
    assert _deliveries(tp) == [
        ("email", "bea.booker@example.com", "Bea Booker (booking contact)", sent, ""),
        ("email", "cal@example.com", "Cal Couple (Couple)", sent, ""),
        ("sms", "+12025550100", "Bea Booker (booking contact)", sent, ""),
        ("sms", "+12025550111", "Cal Couple (Couple)", sent, ""),
    ]
    d = Delivery.objects.filter(touchpoint=tp).first()
    assert d.message == row and d.provider_id.startswith("uid-") and d.combine_key == ""
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT and tp.error == "" and tp.sent_at is not None
    assert tp.podium_message_uid.startswith("uid-")


def test_the_same_address_twice_gets_one_copy():
    lead = _booked()
    LeadContact.objects.create(lead=lead, contact=lead.contact, role=LeadContact.Role.COUPLE)
    tp = _tp(_added(recipients=["booking_contact", "role:couple"]), lead)

    with patch(SEND, side_effect=_ok) as send:
        sender.deliver(tp)

    assert send.call_count == 2
    assert {d.recipient_label for d in Delivery.objects.filter(touchpoint=tp)} == {
        "Bea Booker (booking contact)"
    }


def test_a_missing_address_is_a_skipped_delivery_with_a_reason():
    lead = _booked(email="")
    tp = _tp(_added(), lead)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True

    assert _calls(send) == [("phone", "+12025550100")]
    assert ("email", "", "Bea Booker (booking contact)", "skipped", "No email on file") in (
        _deliveries(tp)
    )
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT


def test_a_text_only_recipient_has_no_email_reason():
    lead = _booked()
    trip = _trip(lead)
    PassengerFactory(reservation=trip, name="Dana Whitfield", mobile="+12025550134", is_lead=True)
    tp = _tp(_added(trigger="status_on_the_way", recipients=["passengers"]), lead, trip)

    with patch(SEND, side_effect=_ok) as send:
        sender.deliver(tp)

    assert _calls(send) == [("phone", "+12025550134")]
    assert _deliveries(tp) == [
        ("sms", "+12025550134", "Dana Whitfield (lead passenger)", "sent", ""),
    ]


def test_nobody_resolved_is_no_recipient():
    lead = _booked()
    tp = _tp(_added(recipients=[]), lead)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, "No recipient")
    assert not Delivery.objects.filter(touchpoint=tp).exists()


def test_people_but_no_address_is_todays_no_usable_channel():
    lead = _booked(phone="", email="")
    tp = _tp(_added(), lead)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, "recipient has no usable channel")
    assert [d[4] for d in _deliveries(tp)] == ["No email on file", "No mobile on file"]


def test_a_rerun_only_sends_the_missing_addresses():
    lead = _booked()
    row = _added()
    tp = _tp(row, lead)
    DeliveryFactory(
        touchpoint=tp,
        message=row,
        channel="sms",
        address="+12025550100",
        status=Delivery.Status.SENT,
    )

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True

    assert _calls(send) == [("email", "bea.booker@example.com")]
    assert Delivery.objects.filter(touchpoint=tp).count() == 2


def test_a_rerun_after_everything_went_is_sent_and_sends_nothing():
    lead = _booked()
    row = _added(send_email=False)
    tp = _tp(row, lead)
    DeliveryFactory(
        touchpoint=tp, message=row, channel="sms", address="+12025550100", status="sent"
    )

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT


def test_a_provider_error_fails_the_delivery_with_its_text():
    lead = _booked()
    tp = _tp(_added(), lead)

    with patch(SEND, side_effect=PodiumAPIError("422 invalid number")):
        assert sender.deliver(tp) is False

    assert {(d[0], d[3], d[4]) for d in _deliveries(tp)} == {
        ("sms", "failed", "422 invalid number"),
        ("email", "failed", "422 invalid number"),
    }
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.FAILED
    assert tp.error == "sms: 422 invalid number; email: 422 invalid number"


def test_one_channel_failing_still_marks_it_sent():
    lead = _booked()
    tp = _tp(_added(), lead)

    def flaky(*, channel_type, **_):
        if channel_type == "email":
            raise PodiumAPIError("mailbox full")
        return {"uid": "m-1"}

    with patch(SEND, side_effect=flaky):
        assert sender.deliver(tp) is True

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT and tp.error == ""
    assert ("email", "failed", "mailbox full") in {(d[0], d[3], d[4]) for d in _deliveries(tp)}


# --- checks --------------------------------------------------------------------------


def test_a_skip_closes_it_with_the_reason():
    lead = LeadFactory(status=Lead.Status.LOST)
    tp = _tp(_row("tp1_welcome"), lead)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, "lead LOST")


def test_a_wait_leaves_it_scheduled_and_says_why(settings):
    settings.PUBLIC_BASE_URL = ""
    lead = LeadFactory(status=Lead.Status.QUOTED)
    tp = _tp(_row("tp3_quote_sent_sms"), lead)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SCHEDULED, "waiting for PUBLIC_BASE_URL")


def test_a_held_review_request_is_cancelled():
    lead = _booked()
    with patch.object(rules, "_review_hold_reason", return_value="complaint logged"):
        tp = _tp(_row("review_request"), lead)
        assert sender.deliver(tp) is False
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (
        TouchPoint.Status.CANCELLED,
        "Review request held: complaint logged",
    )


# --- old scheduling: TouchPoints without a message -----------------------------------


def test_a_touchpoint_without_a_message_is_sent_by_its_kind():
    lead = LeadFactory(contact=ContactFactory(phone="+12025550100", email="a@example.com"))
    tp = _tp(_row("tp1_welcome"), lead, legacy=True)

    with patch(SEND, side_effect=_ok):
        assert sender.deliver(tp) is True

    tp.refresh_from_db()
    assert tp.message == _row("tp1_welcome")
    assert Delivery.objects.filter(touchpoint=tp, message=_row("tp1_welcome")).count() == 2


def test_an_old_thank_you_on_a_wedding_order_gets_the_wedding_message():
    lead = _booked()
    _trip(lead, day=timezone.localdate() - timedelta(days=1), wedding=True)
    tp = _tp(_row("thank_you"), lead, legacy=True)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True

    tp.refresh_from_db()
    assert tp.message == _row("thank_you_wedding")
    # The kind stays: `schedule_post_trip` keys its "already queued" check on it.
    assert tp.kind == "thank_you"
    assert all("Congratulations on the wedding" in c.kwargs["body"] for c in send.call_args_list)


def test_an_old_thank_you_on_a_plain_order_gets_the_plain_message():
    lead = _booked()
    _trip(lead, day=timezone.localdate() - timedelta(days=1))
    tp = _tp(_row("thank_you"), lead, legacy=True)

    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)

    tp.refresh_from_db()
    assert tp.message == _row("thank_you")


# --- combining -----------------------------------------------------------------------


def test_two_trips_one_day_send_one_message_listing_both():
    lead = _booked()
    afternoon = _trip(lead, at=time(16, 0))
    morning = _trip(lead, at=time(10, 0))
    row = _pickup_row()
    first = _tp(row, lead, morning)
    second = _tp(row, lead, afternoon, due=False)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(first) is True

    sms = next(c.kwargs["body"] for c in send.call_args_list if c.kwargs["channel_type"] == "phone")
    assert sms.startswith("Bea, your trips:")
    assert sms.index("10:00 AM") < sms.index("4:00 PM")
    assert {d.combine_key for d in Delivery.objects.filter(touchpoint=first)} == {
        NOV_14.isoformat()
    }

    # Its own time comes (a bulk re-date, so the snapshot still matches): the claim only
    # takes a due row.
    TouchPoint.objects.filter(pk=second.pk).update(scheduled_for=timezone.now())
    with patch(SEND) as send:
        assert sender.deliver(second) is False

    send.assert_not_called()
    second.refresh_from_db()
    assert (second.status, second.error) == (TouchPoint.Status.SKIPPED, rules.COVERED)
    assert {(d[3], d[4]) for d in _deliveries(second)} == {("skipped", "Already sent for Nov 14")}


def test_a_trip_someone_else_gets_is_not_on_the_sheet():
    lead = _booked()
    mine = _trip(lead, at=time(10, 0))
    other_lead = _booked(name="Ola Other", phone="+12025550199", email="ola@example.com")
    theirs = _trip(other_lead, at=time(16, 0))
    row = _pickup_row(send_email=False)
    tp = _tp(row, lead, mine)
    _tp(row, other_lead, theirs, due=False)

    with patch(SEND, side_effect=_ok) as send:
        sender.deliver(tp)

    assert "4:00 PM" not in send.call_args.kwargs["body"]


def test_a_second_recipient_on_the_later_trip_still_gets_it():
    lead = _booked()
    morning = _trip(lead, at=time(10, 0))
    afternoon = _trip(lead, at=time(16, 0))
    PassengerFactory(reservation=afternoon, name="Pia", mobile="+12025550155", is_lead=True)
    row = _pickup_row(recipients=["passengers"], send_email=False)
    with patch(SEND, side_effect=_ok):
        sender.deliver(_tp(row, lead, morning))
        later = _tp(row, lead, afternoon)
        assert sender.deliver(later) is True

    assert _deliveries(later) == [("sms", "+12025550155", "Pia (lead passenger)", "sent", "")]


def test_a_wedding_and_a_plain_trip_the_same_day_get_two_confirmations():
    casey = ContactFactory(name="Casey Morgan", phone="+12025550102", email="casey@example.com")
    pat = ContactFactory(name="Pat Okafor", phone="+12025550103", email="pat@example.com")
    wedding_lead = LeadFactory(status=Lead.Status.BOOKED, contact=casey, billing_contact=pat)
    plain_lead = LeadFactory(status=Lead.Status.BOOKED, contact=casey)
    wedding = _trip(wedding_lead, wedding=True)
    plain = _trip(plain_lead)
    row = _row("trip_confirm_customer")

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(_tp(row, wedding_lead, wedding)) is True
        assert sender.deliver(_tp(row, plain_lead, plain)) is True

    assert _calls(send) == [
        ("email", "casey@example.com"),
        ("email", "pat@example.com"),
        ("phone", "+12025550102"),
        ("phone", "+12025550103"),
    ]


def test_a_cancelled_touchpoints_send_does_not_cover_its_replacement():
    """A pickup time change cancels the sent confirmation and queues a fresh one (today's
    `reschedule_service_touchpoints`): the replacement must go out."""
    lead = _booked()
    trip = _trip(lead)
    row = _row("trip_confirm_customer")
    old = _tp(row, lead, trip)
    with patch(SEND, side_effect=_ok):
        sender.deliver(old)
    TouchPoint.objects.filter(pk=old.pk).update(status=TouchPoint.Status.CANCELLED)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(_tp(row, lead, trip)) is True
    assert send.call_count == 2


def _coordinated(lead) -> Contact:
    coordinator = ContactFactory(name="Dee Coordinator", phone="+12025550144", email="dee@x.com")
    LeadContact.objects.create(
        lead=lead, contact=coordinator, role=LeadContact.Role.DAY_OF_COORDINATOR
    )
    return coordinator


def _dispatched_with(trip, name: str, cell: str) -> Reservation:
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    dispatch_services.set_driver_info(
        a, name=name, cell=cell, vehicle_desc="Suburban", vehicle_number="9"
    )
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.DISPATCHED)
    return Reservation.objects.get(pk=trip.pk)


def test_an_added_driver_row_sends_each_trip_its_own_driver():
    """A coordinator with two dispatched cars that day hears about both drivers: a row on
    the dispatched pickup trigger never stands in for another trip."""
    lead = _booked()
    _coordinated(lead)
    morning = _dispatched_with(_trip(lead, at=time(10, 0)), "Sam Rivera", "+15715551212")
    afternoon = _dispatched_with(_trip(lead, at=time(16, 0)), "Kim Lane", "+15715553434")
    row = _added(
        trigger="pickup_dispatched",
        section=MessageTemplate.Section.TRIP_DAY,
        recipients=["role:day_of_coordinator"],
        send_email=False,
        sms_body="Your driver is {driver_name}, {driver_cell}",
        timing_amount=12,
        timing_unit="hours",
        timing_direction=MessageTemplate.Direction.BEFORE,
    )

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(_tp(row, lead, morning)) is True
        assert sender.deliver(_tp(row, lead, afternoon)) is True

    bodies = [c.kwargs["body"] for c in send.call_args_list]
    assert len(bodies) == 2
    assert "Sam Rivera" in bodies[0] and "Kim Lane" in bodies[1]


def test_an_added_pickup_row_listing_the_day_sends_one_message():
    lead = _booked()
    _coordinated(lead)
    morning, afternoon = _trip(lead, at=time(10, 0)), _trip(lead, at=time(16, 0))
    row = _pickup_row(recipients=["role:day_of_coordinator"], send_email=False)

    first, later = _tp(row, lead, morning), _tp(row, lead, afternoon, due=False)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(first) is True
        TouchPoint.objects.filter(pk=later.pk).update(scheduled_for=timezone.now())
        assert sender.deliver(later) is False

    (body,) = [c.kwargs["body"] for c in send.call_args_list]
    assert body.index("10:00 AM") < body.index("4:00 PM")
    assert {(d[3], d[4]) for d in _deliveries(later)} == {("skipped", "Already sent for Nov 14")}


@pytest.mark.parametrize(
    "kw",
    [
        {"sms_body": "Pickup {trip_pickup_time}", "email_body": "Pickup {trip_pickup_time}"},
        {
            "audience": MessageTemplate.Audience.AFFILIATE,
            "recipients": ["company"],
            "sms_body": "Your trips:\n{trip_sheet}",
            "email_body": "{trip_sheet}",
        },
    ],
    ids=["no-trip-list", "affiliate"],
)
def test_an_added_pickup_row_without_the_day_list_sends_per_trip(kw):
    """Only a wording that lists the person's whole day (`{trip_sheet}`) can stand in for
    the other trips that day; an affiliate's sheet is always per trip."""
    lead = _booked()
    morning, afternoon = _trip(lead, at=time(10, 0)), _trip(lead, at=time(16, 0))
    if kw.get("recipients") == ["company"]:
        vendor = AssignmentFactory(reservation=morning, status=Assignment.Status.CONFIRMED).vendor
        AssignmentFactory(reservation=afternoon, vendor=vendor, status=Assignment.Status.CONFIRMED)
    row = _added(
        trigger="pickup",
        section=MessageTemplate.Section.TRIP_DAY,
        timing_amount=72,
        timing_unit="hours",
        timing_direction=MessageTemplate.Direction.BEFORE,
        **kw,
    )
    first, later = _tp(row, lead, morning), _tp(row, lead, afternoon, due=False)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(first) is True
        TouchPoint.objects.filter(pk=later.pk).update(scheduled_for=timezone.now())
        assert sender.deliver(later) is True

    sms = [c.kwargs["body"] for c in send.call_args_list if c.kwargs["channel_type"] == "phone"]
    assert len(sms) == 2
    assert "10:00 AM" in sms[0] and "4:00 PM" in sms[1]
    assert "4:00 PM" not in sms[0]


def test_per_trip_events_never_combine():
    lead = _booked()
    a, b = _trip(lead, at=time(10, 0)), _trip(lead, at=time(16, 0))
    row = _added(trigger="status_on_the_way", send_email=False)
    with patch(SEND, side_effect=_ok) as send:
        sender.deliver(_tp(row, lead, a))
        sender.deliver(_tp(row, lead, b))
    assert send.call_count == 2


# --- context -------------------------------------------------------------------------


def test_context_greets_the_recipient_not_the_booker():
    lead = _booked()
    tp = _tp(_added(), lead)
    pia = Recipient("Pia Passenger", "Pia Passenger (passenger)", "+12025550155", "", None)
    assert sender.context(tp, tp.message, pia)["first_name"] == "Pia"
    nameless = Recipient("", "+12025550155 (passenger)", "+12025550155", "", None)
    assert sender.context(tp, tp.message, nameless)["first_name"] == "there"


def test_context_for_an_affiliate_keeps_the_customer_fields():
    lead = _booked()
    row = _added(audience=MessageTemplate.Audience.AFFILIATE, trigger="affiliate_accepted")
    tp = _tp(row, lead, _trip(lead))
    vendor = Recipient("Acme Coach", "Acme Coach (company)", "", "ops@acme.test", None)
    assert sender.context(tp, row, vendor)["first_name"] == "Bea"


def test_context_fills_links():
    lead = _booked()
    tp = _tp(_row("trip_confirm_customer"), lead, _trip(lead))
    ctx = sender.context(tp, tp.message, Recipient("Bea", "Bea", "", "", lead.contact_id))
    assert ctx["confirm_link"].startswith(BASE) and ctx["quote_link"].startswith(BASE)


# --- driver details, review request ----------------------------------------------------


def test_the_driver_message_records_the_details_it_carried():
    lead = _booked()
    trip = _trip(lead, day=timezone.localdate() + timedelta(days=1))
    a = AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
    dispatch_services.set_driver_info(
        a, name="Sam Rivera", cell="+15715551212", vehicle_desc="Suburban", vehicle_number="9"
    )
    Reservation.objects.filter(pk=trip.pk).update(trip_status=Reservation.TripStatus.DISPATCHED)
    tp = _tp(_row("driver_released"), lead, Reservation.objects.get(pk=trip.pk))

    with patch(SEND, side_effect=_ok):
        assert sender.deliver(tp) is True

    tp.refresh_from_db()
    a.refresh_from_db()
    assert tp.driver_details == a.driver_info and tp.driver_details["name"] == "Sam Rivera"


def test_the_review_request_uses_podiums_invite():
    lead = _booked(name="Dana Whitfield")
    tp = _tp(_row("review_request"), lead)

    with (
        patch(SEND, return_value={"uid": "m-9"}) as send,
        patch(INVITE, return_value={"url": "https://podium.test/r/1", "uid": "inv-1"}) as invite,
    ):
        assert sender.deliver(tp) is True

    invite.assert_called_once_with(phone="+12025550100", first_name="Dana", last_name="Whitfield")
    assert send.call_args.kwargs["channel_type"] == "phone"
    assert "https://podium.test/r/1" in send.call_args.kwargs["body"]
    review = Review.objects.get(lead=lead)
    assert (review.contact, review.podium_review_invite_uid) == (lead.contact, "inv-1")
    assert review.delivery_status == Review.DeliveryStatus.SENT
    assert _deliveries(tp) == [
        ("sms", "+12025550100", "Dana Whitfield (booking contact)", "sent", ""),
    ]
    tp.refresh_from_db()
    assert (tp.status, tp.podium_message_uid) == (TouchPoint.Status.SENT, "m-9")


def test_a_failed_review_invite_fails_with_todays_error():
    lead = _booked()
    tp = _tp(_row("review_request"), lead)

    with patch(SEND) as send, patch(INVITE, side_effect=PodiumAPIError("boom")):
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.FAILED, "boom")
    assert not Review.objects.filter(lead=lead).exists()
    assert [(d[3], d[4]) for d in _deliveries(tp)] == [("failed", "boom")]


# --- transport -----------------------------------------------------------------------


def test_planner_email_goes_through_postmark():
    lead = _booked()
    planner = ContactFactory(name="Wendy Planner")
    PlannerAccount.objects.create(contact=planner, email="wendy@plans.test")
    LeadContact.objects.create(lead=lead, contact=planner, role=LeadContact.Role.LEAD_PLANNER)
    row = _added(audience=MessageTemplate.Audience.PLANNER, recipients=["planner"])
    tp = _tp(row, lead)

    with patch(SEND) as send, patch(POSTMARK, return_value="pm-1") as mail:
        assert sender.deliver(tp) is True

    send.assert_not_called()
    kwargs = mail.call_args.kwargs
    assert (kwargs["to"], kwargs["subject"], kwargs["template"], kwargs["internal"]) == (
        "wendy@plans.test",
        "Hello Wendy",
        "message",
        False,
    )
    assert kwargs["context"]["body"] == "Email for Wendy"
    assert _deliveries(tp) == [
        ("email", "wendy@plans.test", "Wendy Planner (planner)", "sent", ""),
    ]
    assert Delivery.objects.get(touchpoint=tp).provider_id == "pm-1"


def test_staff_email_is_internal():
    lead = _booked()
    row = _added(audience=MessageTemplate.Audience.STAFF, staff_emails=["Ops@apc.test"])
    tp = _tp(row, lead)

    with patch(POSTMARK, return_value="pm-2") as mail:
        assert sender.deliver(tp) is True

    assert mail.call_args.kwargs["internal"] is True
    assert mail.call_args.kwargs["to"] == "Ops@apc.test"


def test_postmark_refusing_fails_the_delivery():
    lead = _booked()
    row = _added(audience=MessageTemplate.Audience.STAFF, staff_emails=["ops@apc.test"])
    tp = _tp(row, lead)

    with patch(POSTMARK, return_value=None):
        assert sender.deliver(tp) is False

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.FAILED
    assert [(d[3], d[4]) for d in _deliveries(tp)] == [("failed", "Email not sent")]


# --- the cron ------------------------------------------------------------------------


def test_the_cron_job_is_the_shared_sender():
    assert cron.JOBS["run-touchpoints"] is sender.run_touchpoints


def test_run_sends_due_rows_and_counts_them():
    lead = _booked()
    due = _tp(_added(), lead)
    later = _tp(_added(), lead, due=False)

    with patch(SEND, side_effect=_ok):
        assert sender.run_touchpoints() == 1

    due.refresh_from_db()
    later.refresh_from_db()
    assert (due.status, later.status) == (TouchPoint.Status.SENT, TouchPoint.Status.SCHEDULED)


def test_run_does_nothing_while_touchpoints_are_off(settings):
    settings.TOUCHPOINTS_ENABLED = False
    tp = _tp(_added(), _booked())

    with patch(SEND) as send:
        assert sender.run_touchpoints() == 0

    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED


def test_outbound_off_closes_due_rows_unsent(settings):
    settings.OUTBOUND_ENABLED = False
    lead = _booked()
    due = _tp(_added(), lead)
    later = _tp(_added(), lead, due=False)

    with patch(SEND) as send:
        assert sender.run_touchpoints() == 0

    send.assert_not_called()
    due.refresh_from_db()
    later.refresh_from_db()
    assert (due.status, due.error) == (TouchPoint.Status.SKIPPED, sender.OUTBOUND_OFF)
    assert sender.OUTBOUND_OFF == "outbound messaging off"
    assert later.status == TouchPoint.Status.SCHEDULED


def test_one_bad_row_never_kills_the_run():
    lead = _booked()
    bad = _tp(_added(), lead)
    good = _tp(_added(), lead)
    real = sender.deliver

    def deliver(tp, **kw):
        if tp.pk == bad.pk:
            raise RuntimeError("bad row")
        return real(tp, **kw)

    with patch.object(sender, "deliver", side_effect=deliver), patch(SEND, side_effect=_ok):
        assert sender.run_touchpoints() == 1

    bad.refresh_from_db()
    good.refresh_from_db()
    assert (bad.status, bad.error) == (TouchPoint.Status.FAILED, "unexpected error")
    assert good.status == TouchPoint.Status.SENT


def test_send_due_sends_only_the_given_due_rows():
    lead = _booked()
    a, b = _tp(_added(), lead), _tp(_added(), lead)
    later = _tp(_added(), lead, due=False)

    with patch(SEND, side_effect=_ok):
        sender.send_due([a.pk, later.pk])

    for tp in (a, b, later):
        tp.refresh_from_db()
    assert [a.status, b.status, later.status] == ["sent", "scheduled", "scheduled"]


def test_send_due_respects_the_switches(settings):
    lead = _booked()
    tp = _tp(_added(), lead)
    settings.TOUCHPOINTS_ENABLED = False
    with patch(SEND) as send:
        sender.send_due([tp.pk])
    send.assert_not_called()

    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = False
    sender.send_due([tp.pk])
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, sender.OUTBOUND_OFF)


def test_send_due_survives_a_bad_row():
    lead = _booked()
    tp = _tp(_added(), lead)
    with patch.object(sender, "deliver", side_effect=RuntimeError("bad")):
        sender.send_due([tp.pk])
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.FAILED, "unexpected error")


def _confirmations(n: int, *, first: int = 0) -> None:
    """`n` due 72h confirmations on `n` orders, every other one queued by the old scheduler."""
    row = _row("trip_confirm_customer")
    for i in range(first, first + n):
        lead = _booked(name=f"Person {i}", phone=f"+1202555{i:04d}", email=f"p{i}@example.com")
        trip = _trip(lead, at=time(8 + i % 12, 0))
        PassengerFactory(reservation=trip, name=f"Pax {i}", mobile="", is_lead=False)
        AssignmentFactory(reservation=trip, status=Assignment.Status.CONFIRMED)
        _tp(row, lead, trip, legacy=i % 2 == 0)


def test_run_query_count_is_bounded(django_assert_max_num_queries):
    _confirmations(10)

    with patch(SEND, side_effect=_ok), django_assert_max_num_queries(QUERY_BUDGET):
        assert sender.run_touchpoints() == 10


def test_run_cost_per_row_is_constant():
    """One row, then ten: the extra cost of each further row stays under a fixed bound."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _confirmations(1)
    with patch(SEND, side_effect=_ok), CaptureQueriesContext(connection) as one:
        assert sender.run_touchpoints() == 1
    _confirmations(10, first=1)
    with patch(SEND, side_effect=_ok), CaptureQueriesContext(connection) as ten:
        assert sender.run_touchpoints() == 10

    assert (len(ten) - len(one)) / 9 <= PER_ROW


# Measured at 101 for ten rows, about nine each: claim it; one read of its own and its day's
# Deliveries; the confirmation's day group (read once per send) with its stops; the old
# engine's sends that day; two Delivery inserts; the TouchPoint save; and for half the rows
# the message pointer. The batch itself costs about ten.
PER_ROW = 9
QUERY_BUDGET = 10 * PER_ROW + 15


# --- fix round 1 ---------------------------------------------------------------------


def test_an_old_engine_send_covers_the_rest_of_the_day():
    """At cut-over: the old engine sent trip A's confirmation (no Deliveries); trip B's,
    still queued, stands down as it does today."""
    lead = _booked()
    morning, afternoon = _trip(lead, at=time(10, 0)), _trip(lead, at=time(16, 0))
    row = _row("trip_confirm_customer")
    TouchPointFactory(
        lead=lead,
        reservation=morning,
        kind=row.key,
        status=TouchPoint.Status.SENT,
        scheduled_for=timezone.now() - timedelta(hours=1),
    )
    tp = _tp(row, lead, afternoon, legacy=True)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SKIPPED, rules.COVERED)
    assert {(d[3], d[4]) for d in _deliveries(tp)} == {("skipped", "Already sent for Nov 14")}


def test_an_old_engine_send_on_another_day_does_not_cover():
    lead = _booked()
    other_day = _trip(lead, day=NOV_14 + timedelta(days=1))
    trip = _trip(lead)
    row = _row("trip_confirm_customer")
    TouchPointFactory(lead=lead, reservation=other_day, kind=row.key, status=TouchPoint.Status.SENT)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(_tp(row, lead, trip)) is True
    assert send.call_count == 2


def test_two_deliver_calls_on_one_snapshot_send_once():
    lead = _booked()
    tp = _tp(_added(), lead)
    # As a batch loads it: deliveries prefetched (none yet) before the other send ran.
    stale = TouchPoint.objects.prefetch_related("deliveries").get(pk=tp.pk)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True
        assert sender.deliver(stale) is False

    assert send.call_count == 2  # one text, one email: the first call only
    stale.refresh_from_db()
    assert stale.status == TouchPoint.Status.SENT


def test_a_row_being_sent_is_not_sent_again_by_a_fresh_read():
    """The cron loads the row while an inline send is still talking to Podium."""
    lead = _booked()
    tp = _tp(_added(send_email=False), lead)
    nested = []

    def send(**_):
        if not nested:
            nested.append(sender.deliver(TouchPoint.objects.get(pk=tp.pk)))
        return {"uid": "m-1"}

    with patch(SEND, side_effect=send) as podium_send:
        assert sender.deliver(tp) is True

    assert nested == [False]
    assert podium_send.call_count == 1


def test_a_rerun_reads_its_deliveries_fresh():
    lead = _booked()
    row = _added(send_email=False)
    tp = TouchPoint.objects.prefetch_related("deliveries").get(pk=_tp(row, lead).pk)
    list(tp.deliveries.all())  # an empty prefetch, now stale
    DeliveryFactory(
        touchpoint=tp, message=row, channel="sms", address="+12025550100", status="sent"
    )

    with patch(SEND) as send:
        sender.deliver(tp)
    send.assert_not_called()


def test_a_crash_after_a_send_leaves_it_sent():
    lead = _booked()
    tp = _tp(_added(), lead)
    calls = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) > 1:
            raise RuntimeError("socket closed")
        return {"uid": "m-1"}

    with patch(SEND, side_effect=flaky):
        sender.run_touchpoints()

    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT and tp.error == ""


def test_contexts_are_built_before_anything_is_sent():
    lead = _booked()
    couple = ContactFactory(name="Cal Couple", phone="+12025550111", email="cal@example.com")
    LeadContact.objects.create(lead=lead, contact=couple, role=LeadContact.Role.COUPLE)
    tp = _tp(_added(recipients=["booking_contact", "role:couple"]), lead)
    real = sender.context
    built = []

    def context(*args):
        built.append(args)
        if len(built) > 1:
            raise RuntimeError("bad wording")
        return real(*args)

    with (
        patch(SEND, side_effect=_ok) as send,
        patch.object(sender, "context", side_effect=context),
        pytest.raises(RuntimeError),
    ):
        sender.deliver(tp)
    send.assert_not_called()


def test_an_old_thank_you_is_pointed_at_its_message_only_when_it_sends(settings):
    settings.PUBLIC_BASE_URL = ""
    lead = _booked()
    _trip(lead, day=timezone.localdate() - timedelta(days=1), wedding=True)
    tp = _tp(_row("thank_you"), lead, legacy=True)

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED and tp.message_id is None


def test_a_row_changed_since_it_was_read_is_left_alone():
    """Re-dated after the batch loaded it: the stale copy must not send it early."""
    lead = _booked()
    tp = _tp(_added(), lead)
    fresh = TouchPoint.objects.get(pk=tp.pk)
    fresh.scheduled_for = timezone.now() + timedelta(days=2)
    fresh.save(update_fields=["scheduled_for", "updated_at"])

    with patch(SEND) as send:
        assert sender.deliver(tp) is False

    send.assert_not_called()
    assert tp.scheduled_for > timezone.now()  # refreshed


# --- day-one routing (the client-approved routing table) ------------------------------


def test_day_one_routing_of_the_wedding_details_and_driver_messages():
    """Pinned from the routing table the client approved: the wedding details request goes
    to the booking contact, the couple and the lead planner; the driver message to the
    booking contact, the day-of coordinator and the transportation contact. One copy per
    address: the booker who is also one of the couple hears once."""
    lead = _booked()
    people = {
        LeadContact.Role.COUPLE: ("Cal Couple", "+12025550111", "cal@example.com"),
        LeadContact.Role.LEAD_PLANNER: ("Pia Planner", "+12025550122", "pia@example.com"),
        LeadContact.Role.DAY_OF_COORDINATOR: ("Dee Day", "+12025550133", "dee@example.com"),
        LeadContact.Role.TRANSPORTATION: ("Tom Trans", "+12025550144", "tom@example.com"),
        LeadContact.Role.EMERGENCY: ("Eve Emergency", "+12025550155", "eve@example.com"),
    }
    for role, (name, phone, email) in people.items():
        contact = ContactFactory(name=name, phone=phone, email=email)
        LeadContact.objects.create(lead=lead, contact=contact, role=role)
    LeadContact.objects.create(lead=lead, contact=lead.contact, role=LeadContact.Role.COUPLE)
    trip = _dispatched_with(_trip(lead, wedding=True), "Sam Rivera", "+15715551212")

    def reached(key: str) -> list[tuple[str, str]]:
        with patch(SEND, side_effect=_ok) as send:
            assert sender.deliver(_tp(_row(key), lead, trip)) is True
        return _calls(send)

    assert reached("wed_final_details") == [
        ("email", "bea.booker@example.com"),
        ("email", "cal@example.com"),
        ("email", "pia@example.com"),
        ("phone", "+12025550100"),
        ("phone", "+12025550111"),
        ("phone", "+12025550122"),
    ]
    assert reached("driver_released") == [
        ("email", "bea.booker@example.com"),
        ("email", "dee@example.com"),
        ("email", "tom@example.com"),
        ("phone", "+12025550100"),
        ("phone", "+12025550133"),
        ("phone", "+12025550144"),
    ]
