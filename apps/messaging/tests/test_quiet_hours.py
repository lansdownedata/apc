"""Quiet hours hold texts (spec C section 5, story 5, APC-114).

A scheduled text whose send time falls inside `NotificationConfig`'s window, judged in the
trip's own zone, waits for the window's end; its email goes now. Staff clicks, staff mail
and a pickup before the window ends are sent anyway. Podium and Postmark are always mocked.

The suite runs with quiet hours off (the root conftest), so its sends don't depend on the
hour it runs at; these tests turn them back on with the `quiet_hours` marker and a frozen
clock.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import requests

from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.messaging import events, history, quiet, rules, sender
from apps.messaging.factories import DeliveryFactory, MessageRowFactory, TouchPointFactory
from apps.messaging.models import Delivery, MessageTemplate, NotificationConfig, TouchPoint
from apps.reservations.factories import ReservationFactory

pytestmark = [pytest.mark.django_db, pytest.mark.quiet_hours]

SEND = "apps.integrations.podium.send_message"
INVITE = "apps.integrations.podium.create_review_invitation"
POSTMARK = "apps.notifications.email.deliver_html_email"
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
YEAR = date.today().year + 1
_A = MessageTemplate.Audience


@pytest.fixture(autouse=True)
def live(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example.com"
    settings.TIME_ZONE = "America/New_York"  # the office: Washington


@pytest.fixture
def clock():
    """Freeze `timezone.now`; move it with `clock.now = ...`."""

    class Clock:
        now = datetime(YEAR, 1, 10, 12, 0, tzinfo=ET)

    with patch("django.utils.timezone.now", side_effect=lambda: Clock.now):
        yield Clock


def _window(start=time(21, 0), end=time(8, 0)) -> None:
    NotificationConfig.objects.update_or_create(
        pk=1, defaults={"quiet_start": start, "quiet_end": end}
    )


def _ok(*_, **__):
    _ok.n = getattr(_ok, "n", 0) + 1
    return {"uid": f"uid-{_ok.n}"}


def _booked(name="Bea Booker", phone="+12025550100", email="bea.booker@example.com"):
    return LeadFactory(
        status=Lead.Status.BOOKED, contact=ContactFactory(name=name, phone=phone, email=email)
    )


def _trip(lead, at: datetime):
    return ReservationFactory(
        lead=lead,
        pickup_date=at.date(),
        pickup_time=at.timetz().replace(tzinfo=None),
        pickup_timezone=str(at.tzinfo),
    )


def _pickup_row(**kw) -> MessageTemplate:
    """A customer text and email on the combining pickup trigger, 72 hours before."""
    kw.setdefault("recipients", ["booking_contact"])
    kw.setdefault("subject", "Your trips")
    kw.setdefault("email_body", "{trip_sheet}")
    kw.setdefault("sms_body", "{first_name}, your trips:\n{trip_sheet}")
    kw.setdefault("timing_amount", 72)
    kw.setdefault("timing_direction", MessageTemplate.Direction.BEFORE)
    return MessageRowFactory(
        trigger="pickup", section=MessageTemplate.Section.TRIP_DAY, timing_unit="hours", **kw
    )


def _tp(row, lead, trip=None) -> TouchPoint:
    from django.utils import timezone

    tp = TouchPointFactory(
        lead=lead,
        reservation=trip,
        kind=row.key,
        message=row,
        scheduled_for=timezone.now() - timedelta(minutes=1),
    )
    return TouchPoint.objects.select_related("lead", "reservation", "message").get(pk=tp.pk)


def _deliveries(tp) -> list[tuple]:
    return sorted(
        (d.channel, d.address, d.status, d.reason) for d in Delivery.objects.filter(touchpoint=tp)
    )


def _texts(send) -> list[str]:
    return [
        c.kwargs["identifier"] for c in send.call_args_list if c.kwargs["channel_type"] == "phone"
    ]


# --- hold_until ------------------------------------------------------------------------


def test_la_pickup_held_to_8am_pacific(clock):
    """Washington staff, an LA pickup: a 10 PM PT send waits for 8:00 AM PT, not ET."""
    _window()
    lead = _booked()
    trip = _trip(lead, datetime(YEAR, 1, 12, 14, 0, tzinfo=PT))
    row = _pickup_row()
    tp = _tp(row, lead, trip)
    morning = datetime(YEAR, 1, 11, 8, 0, tzinfo=PT)

    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 10, 22, 0, tzinfo=PT)) == morning
    # 8 PM PT is 11 PM in Washington: the trip's zone decides, so it is not held.
    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 10, 20, 0, tzinfo=PT)) is None

    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=PT)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True  # the email went

    assert _texts(send) == []
    tp.refresh_from_db()
    assert (tp.status, tp.scheduled_for) == (TouchPoint.Status.SCHEDULED, morning)
    assert _deliveries(tp) == [
        ("email", "bea.booker@example.com", "sent", ""),
        ("sms", "+12025550100", "queued", "Quiet hours until 8:00 AM PST"),
    ]


def test_window_crossing_midnight_both_sides():
    _window()
    lead = _booked()
    trip = _trip(lead, datetime(YEAR, 1, 14, 14, 0, tzinfo=ET))
    row = _pickup_row()
    tp = _tp(row, lead, trip)

    def held(h, m, day=11):
        return quiet.hold_until(tp, row, now=datetime(YEAR, 1, day, h, m, tzinfo=ET))

    assert held(20, 59) is None
    assert held(21, 0) == datetime(YEAR, 1, 12, 8, 0, tzinfo=ET)
    assert held(23, 30) == datetime(YEAR, 1, 12, 8, 0, tzinfo=ET)
    assert held(6, 0) == datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    assert held(7, 59) == datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    assert held(8, 0) is None


def test_a_window_inside_one_day():
    _window(start=time(1, 0), end=time(5, 0))
    lead = _booked()
    row = _pickup_row()
    tp = _tp(row, lead, _trip(lead, datetime(YEAR, 1, 14, 14, 0, tzinfo=ET)))

    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 11, 0, 59, tzinfo=ET)) is None
    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 11, 3, 0, tzinfo=ET)) == datetime(
        YEAR, 1, 11, 5, 0, tzinfo=ET
    )
    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 11, 23, 0, tzinfo=ET)) is None


def test_pickup_before_window_end_sends_now(clock):
    """A 6:00 AM pickup's driver info, due at 5:00 AM, can't wait for 8:00."""
    _window()
    lead = _booked()
    early = _trip(lead, datetime(YEAR, 1, 11, 6, 0, tzinfo=ET))
    on_the_hour = _trip(lead, datetime(YEAR, 1, 11, 8, 0, tzinfo=ET))
    later = _trip(lead, datetime(YEAR, 1, 11, 8, 30, tzinfo=ET))
    row = MessageRowFactory(
        trigger="pickup_dispatched", recipients=["booking_contact"], sms_body="Driver: Sam"
    )
    five_am = datetime(YEAR, 1, 11, 5, 0, tzinfo=ET)

    assert quiet.hold_until(_tp(row, lead, early), row, now=five_am) is None
    # Held to 8:00 it would arrive as the car does: it goes now too.
    assert quiet.hold_until(_tp(row, lead, on_the_hour), row, now=five_am) is None
    assert quiet.hold_until(_tp(row, lead, later), row, now=five_am) == datetime(
        YEAR, 1, 11, 8, 0, tzinfo=ET
    )

    clock.now = five_am
    row = _pickup_row(send_email=False)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(_tp(row, lead, early)) is True
    assert _texts(send) == ["+12025550100"]


def test_staff_click_never_held(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 23, 0, tzinfo=ET)
    lead = _booked()
    row = MessageRowFactory(
        key="click-quote",
        trigger="send_quote",
        recipients=["booking_contact"],
        send_email=False,
        sms_body="Hi {first_name}: {quote_link}",
    )
    assert quiet.hold_until(_tp(row, lead), row, now=clock.now) is None

    with patch(SEND, side_effect=_ok) as send:
        result = sender.send_now(row.key, lead, extra_ctx={"quote_link": "https://q.test/1"})

    assert result.sent is True
    assert _texts(send) == ["+12025550100"]


def test_a_pickup_already_past_is_held():
    """The exemption is for a pickup still ahead: an "after pickup" text, or a trip-done
    text, for a trip that already ran waits for the morning like any other."""
    _window()
    lead = _booked()
    trip = _trip(lead, datetime(YEAR, 1, 10, 14, 0, tzinfo=ET))
    eleven_pm = datetime(YEAR, 1, 10, 23, 0, tzinfo=ET)
    morning = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    after = _pickup_row(timing_direction=MessageTemplate.Direction.AFTER, timing_amount=9)
    done = MessageRowFactory(trigger="trip_done", recipients=["booking_contact"])

    assert quiet.hold_until(_tp(after, lead, trip), after, now=eleven_pm) == morning
    assert quiet.hold_until(_tp(done, lead, trip), done, now=eleven_pm) == morning


def test_live_trip_status_texts_always_send(clock):
    """Dispatched, On the way and Arrived describe the car right now."""
    _window()
    lead = _booked()
    trip = _trip(lead, datetime(YEAR, 1, 10, 23, 30, tzinfo=ET))
    clock.now = datetime(YEAR, 1, 10, 23, 35, tzinfo=ET)
    for key in ("status_dispatched", "status_on_the_way"):
        row = MessageRowFactory(trigger=key, recipients=["booking_contact"])
        assert quiet.hold_until(_tp(row, lead, trip), row, now=clock.now) is None
    arrived = MessageRowFactory(
        trigger="status_arrived", recipients=["booking_contact"], send_email=False
    )
    tp = _tp(arrived, lead, trip)
    assert quiet.hold_until(tp, arrived, now=clock.now) is None

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True
    assert _texts(send) == ["+12025550100"]


def test_staff_audience_ignores_quiet_hours():
    _window()
    lead = _booked()
    row = _pickup_row(audience=_A.STAFF, recipients=[], staff_emails=["ops@apc.test"])
    tp = _tp(row, lead, _trip(lead, datetime(YEAR, 1, 14, 14, 0, tzinfo=ET)))

    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 10, 23, 0, tzinfo=ET)) is None


def test_start_equals_end_means_off(clock):
    _window(start=time(22, 0), end=time(22, 0))
    clock.now = datetime(YEAR, 1, 10, 23, 0, tzinfo=ET)
    lead = _booked()
    row = _pickup_row(send_email=False)
    tp = _tp(row, lead, _trip(lead, datetime(YEAR, 1, 12, 14, 0, tzinfo=ET)))

    assert quiet.hold_until(tp, row, now=clock.now) is None
    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True
    assert _texts(send) == ["+12025550100"]
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT


def test_an_order_row_uses_the_company_zone(settings):
    settings.TIME_ZONE = "America/Chicago"
    _window()
    lead = _booked()
    row = MessageRowFactory(recipients=["booking_contact"])  # lead_created: no trip
    tp = _tp(row, lead)
    central = ZoneInfo("America/Chicago")

    held = quiet.hold_until(tp, row, now=datetime(YEAR, 1, 10, 21, 30, tzinfo=central))

    assert held == datetime(YEAR, 1, 11, 8, 0, tzinfo=central)
    assert quiet.hold_until(tp, row, now=datetime(YEAR, 1, 10, 20, 30, tzinfo=central)) is None


def _first_sunday(month: int, nth: int = 1) -> date:
    day = date(YEAR, month, 1)
    day += timedelta(days=(6 - day.weekday()) % 7)
    return day + timedelta(weeks=nth - 1)


def test_the_window_end_follows_daylight_saving():
    _window()
    lead = _booked()
    row = _pickup_row()
    fall_back = _first_sunday(11)  # 2:00 AM EDT becomes 1:00 AM EST
    spring_forward = _first_sunday(3, nth=2)
    for change, abbreviation in ((fall_back, "EST"), (spring_forward, "EDT")):
        trip = _trip(lead, datetime.combine(change + timedelta(days=3), time(14, 0), tzinfo=ET))
        tp = _tp(row, lead, trip)
        night = datetime.combine(change - timedelta(days=1), time(22, 0), tzinfo=ET)

        held = quiet.hold_until(tp, row, now=night)

        assert held == datetime.combine(change, time(8, 0), tzinfo=ET)
        assert held.tzname() == abbreviation
        assert quiet.reason(held) == f"Quiet hours until 8:00 AM {abbreviation}"


# --- the sender ------------------------------------------------------------------------


def test_email_part_sends_during_quiet_hours(clock):
    """The email goes at night; the text waits, and the morning run sends only the text,
    settling the same Delivery line."""
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    row = _pickup_row()
    tp = _tp(row, lead, _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET)))

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(tp) is True
    assert [c.kwargs["channel_type"] for c in send.call_args_list] == ["email"]
    queued = Delivery.objects.get(touchpoint=tp, channel="sms")
    assert (queued.status, queued.reason) == ("queued", "Quiet hours until 8:00 AM EST")

    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 0  # still night: nothing is due

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 1

    assert [c.kwargs["channel_type"] for c in send.call_args_list] == ["phone"]
    assert _deliveries(tp) == [
        ("email", "bea.booker@example.com", "sent", ""),
        ("sms", "+12025550100", "sent", ""),
    ]
    queued.refresh_from_db()
    assert queued.status == "sent"  # the queued line itself was settled
    tp.refresh_from_db()
    assert (tp.status, tp.error) == (TouchPoint.Status.SENT, "")


def test_held_text_shows_in_the_order_history(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=PT)
    lead = _booked()
    tp = _tp(
        _pickup_row(send_email=False), lead, _trip(lead, datetime(YEAR, 1, 13, 9, 0, tzinfo=PT))
    )
    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)

    (row,) = history.for_lead(lead)

    assert (row.status, row.reason) == ("Waiting", "Quiet hours until 8:00 AM PST")
    assert row.when_display == "Jan 11, 8:00 AM PST"


def test_held_again_keeps_one_line_per_address(clock):
    """A held row brought forward overnight (a dispatch re-dating it) is held again: still
    one queued line, not two."""
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    tp = _tp(
        _pickup_row(send_email=False), lead, _trip(lead, datetime(YEAR, 1, 13, 9, 0, tzinfo=ET))
    )
    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)

    clock.now = datetime(YEAR, 1, 10, 23, 0, tzinfo=ET)
    TouchPoint.objects.filter(pk=tp.pk).update(scheduled_for=clock.now)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 0

    send.assert_not_called()
    assert _deliveries(tp) == [("sms", "+12025550100", "queued", "Quiet hours until 8:00 AM EST")]
    tp.refresh_from_db()
    assert tp.scheduled_for == datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)


def test_a_failed_email_at_night_is_not_tried_again_in_the_morning(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    tp = _tp(_pickup_row(), lead, _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET)))
    from apps.integrations.podium import PodiumAPIError

    with patch(SEND, side_effect=PodiumAPIError("mailbox full")):
        assert sender.deliver(tp) is False
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=PodiumAPIError("carrier down")) as send:
        sender.run_touchpoints()

    assert [c.kwargs["channel_type"] for c in send.call_args_list] == ["phone"]
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.FAILED
    assert tp.error == "email: mailbox full; sms: carrier down"
    assert [d[2] for d in _deliveries(tp)] == ["failed", "failed"]


def test_a_recipient_gone_by_morning_closes_the_queued_line(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    tp = _tp(_pickup_row(), lead, _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET)))
    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)

    contact = lead.contact
    contact.phone = "+12025550177"  # a new mobile on file overnight
    contact.save()
    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 1

    assert _texts(send) == ["+12025550177"]
    assert _deliveries(tp) == [
        ("email", "bea.booker@example.com", "sent", ""),
        ("sms", "+12025550100", "skipped", sender.NO_LONGER),
        ("sms", "+12025550177", "sent", ""),
    ]


def test_held_texts_survive_a_send_error(clock):
    """An unexpected error on the second email (a timeout Podium's client doesn't wrap)
    must not settle the row SENT and lose the texts already queued."""
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    couple = ContactFactory(name="Cal Couple", phone="+12025550111", email="cal@example.com")
    LeadContact.objects.create(lead=lead, contact=couple, role=LeadContact.Role.COUPLE)
    row = _pickup_row(recipients=["booking_contact", "role:couple"])
    tp = _tp(row, lead, _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET)))

    def flaky(**kw):
        if kw["identifier"] == "cal@example.com":
            raise requests.Timeout("read timed out")
        return _ok()

    with patch(SEND, side_effect=flaky):
        sender.run_touchpoints()
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED
    assert tp.updated_at <= clock.now  # the lease is over

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        sender.run_touchpoints()

    assert sorted(_texts(send)) == ["+12025550100", "+12025550111"]
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    assert [d[:3] for d in _deliveries(tp)] == [
        ("email", "bea.booker@example.com", "sent"),
        ("email", "cal@example.com", "sent"),
        ("sms", "+12025550100", "sent"),
        ("sms", "+12025550111", "sent"),
    ]


def test_an_address_back_on_file_by_morning_still_gets_its_text(clock):
    """A held mobile replaced overnight, then put back: the morning still texts it, on its
    one line."""
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    tp = _tp(
        _pickup_row(send_email=False), lead, _trip(lead, datetime(YEAR, 1, 13, 9, 0, tzinfo=ET))
    )
    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)

    contact = lead.contact
    for phone, at in (("+12025550177", 23), ("+12025550100", 23)):
        contact.phone = phone
        contact.save()
        clock.now = datetime(YEAR, 1, 10, at, 30 if phone.endswith("100") else 0, tzinfo=ET)
        TouchPoint.objects.filter(pk=tp.pk).update(scheduled_for=clock.now)
        with patch(SEND, side_effect=_ok) as send:
            sender.run_touchpoints()
        send.assert_not_called()

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 1

    assert _texts(send) == ["+12025550100"]
    assert _deliveries(tp) == [
        ("sms", "+12025550100", "sent", ""),
        ("sms", "+12025550177", "skipped", sender.NO_LONGER),
    ]


def test_a_text_sent_after_a_hold_keeps_the_emails_own_time_in_history(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    tp = _tp(_pickup_row(), lead, _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET)))
    with patch(SEND, side_effect=_ok):
        sender.deliver(tp)
    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok):
        sender.run_touchpoints()

    rows = {r.to.rsplit(" · ", 1)[1]: r.when_display for r in history.for_lead(lead)}

    assert rows == {"Email": "Jan 10, 10:00 PM EST", "SMS": "Jan 11, 8:00 AM EST"}


def test_the_review_request_is_held_too(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked(name="Dana Whitfield")
    tp = _tp(MessageTemplate.objects.get(key="review_request"), lead)
    invite = {"url": "https://podium.test/r/1", "uid": "inv-1"}

    with patch(SEND, side_effect=_ok) as send, patch(INVITE, return_value=invite) as invited:
        assert sender.deliver(tp) is False
    send.assert_not_called()
    invited.assert_not_called()
    assert _deliveries(tp) == [("sms", "+12025550100", "queued", "Quiet hours until 8:00 AM EST")]

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send, patch(INVITE, return_value=invite) as invited:
        assert sender.run_touchpoints() == 1
    invited.assert_called_once()
    assert _deliveries(tp) == [("sms", "+12025550100", "sent", "")]


# --- combining -------------------------------------------------------------------------


def test_a_held_text_does_not_count_as_sent_for_its_siblings(clock):
    """A late booking at 10 PM: the 6 AM trip's text goes now (its pickup is before the
    window ends) although the 2 PM trip's same-day text is held; in the morning the held one
    finds the day already covered."""
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    early = _trip(lead, datetime(YEAR, 1, 11, 6, 0, tzinfo=ET))
    afternoon = _trip(lead, datetime(YEAR, 1, 11, 14, 0, tzinfo=ET))
    row = _pickup_row(send_email=False)
    held = _tp(row, lead, afternoon)
    goes = _tp(row, lead, early)

    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(held) is False
        assert sender.deliver(goes) is True

    assert _texts(send) == ["+12025550100"]
    body = send.call_args.kwargs["body"]
    assert body.index("6:00 AM") < body.index("2:00 PM")  # the day's whole list

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        sender.run_touchpoints()

    send.assert_not_called()
    held.refresh_from_db()
    assert (held.status, held.error) == (TouchPoint.Status.SKIPPED, rules.COVERED)
    day = f"Jan {afternoon.pickup_date.day}"
    assert _deliveries(held) == [("sms", "+12025550100", "skipped", f"Already sent for {day}")]


def test_two_held_texts_the_same_day_combine_in_the_morning(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    morning = _trip(lead, datetime(YEAR, 1, 13, 10, 0, tzinfo=ET))
    afternoon = _trip(lead, datetime(YEAR, 1, 13, 16, 0, tzinfo=ET))
    row = _pickup_row()
    first, second = _tp(row, lead, morning), _tp(row, lead, afternoon)

    with patch(SEND, side_effect=_ok) as send:
        sender.run_touchpoints()
    # One email for the day, listing both; both texts wait.
    assert [c.kwargs["channel_type"] for c in send.call_args_list] == ["email"]
    assert [d[2] for d in _deliveries(first)] == ["sent", "queued"]
    assert [d[2:] for d in _deliveries(second)] == [
        ("skipped", "Already sent for Jan 13"),
        ("queued", "Quiet hours until 8:00 AM EST"),
    ]

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 1

    assert _texts(send) == ["+12025550100"]
    body = send.call_args.kwargs["body"]
    assert body.index("10:00 AM") < body.index("4:00 PM")
    assert [d[2] for d in _deliveries(first)] == ["sent", "sent"]
    assert [d[2:] for d in _deliveries(second)] == [
        ("skipped", "Already sent for Jan 13"),
        ("skipped", "Already sent for Jan 13"),
    ]
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.status, second.status) == (TouchPoint.Status.SENT, TouchPoint.Status.SKIPPED)


# --- re-dating -------------------------------------------------------------------------


def test_a_pickup_moved_while_held_cancels_the_held_text_and_sends_the_new_one(clock):
    _window()
    clock.now = datetime(YEAR, 1, 10, 22, 0, tzinfo=ET)
    lead = _booked()
    trip = _trip(lead, datetime(YEAR, 1, 13, 14, 0, tzinfo=ET))
    row = _pickup_row(send_email=False)
    old = _tp(row, lead, trip)
    with patch(SEND, side_effect=_ok):
        sender.deliver(old)

    trip.pickup_date += timedelta(days=1)
    trip.save()
    events.reschedule_trip(trip)

    old.refresh_from_db()
    assert old.status == TouchPoint.Status.CANCELLED
    new = TouchPoint.objects.get(message=row, status=TouchPoint.Status.SCHEDULED)
    assert new.scheduled_for == datetime(YEAR, 1, 11, 14, 0, tzinfo=ET)

    clock.now = datetime(YEAR, 1, 11, 8, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.run_touchpoints() == 0  # the cancelled one stays quiet
    send.assert_not_called()

    # The move also queued the seeded confirmation; this row's new send is what matters.
    clock.now = datetime(YEAR, 1, 11, 14, 0, tzinfo=ET)
    with patch(SEND, side_effect=_ok) as send:
        assert sender.deliver(new) is True
    assert _texts(send) == ["+12025550100"]
    assert _deliveries(new) == [("sms", "+12025550100", "sent", "")]

    rows = [r for r in history.for_lead(lead) if r.name == row.name]
    assert sorted((r.status, r.reason) for r in rows) == [("Cancelled", ""), ("Sent", "")]


def test_a_queued_line_on_a_closed_touchpoint_shows_its_status():
    lead = LeadFactory()
    for status, error in (
        (TouchPoint.Status.CANCELLED, ""),
        (TouchPoint.Status.SKIPPED, "lead LOST"),
    ):
        tp = TouchPointFactory(
            lead=lead, message=MessageRowFactory(name=status), status=status, error=error
        )
        DeliveryFactory(
            touchpoint=tp,
            message=tp.message,
            status=Delivery.Status.QUEUED,
            reason="Quiet hours until 8:00 AM EST",
        )

    rows = {r.name: (r.status, r.reason) for r in history.for_lead(lead)}

    assert rows == {"cancelled": ("Cancelled", ""), "skipped": ("Skipped", "lead LOST")}
