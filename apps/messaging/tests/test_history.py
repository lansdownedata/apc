from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.messaging import history, rules, sender
from apps.messaging.factories import DeliveryFactory, MessageRowFactory, TouchPointFactory
from apps.messaging.models import Delivery, TouchPoint
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def logged_in_client(client):
    client.force_login(UserFactory())  # an Agent
    return client


def _tp(lead, **kw):
    kw.setdefault("message", MessageRowFactory(name=kw.pop("name", "Welcome")))
    return TouchPointFactory(lead=lead, **kw)


def test_empty_for_a_lead_with_nothing_sent():
    assert history.for_lead(LeadFactory()) == []


def test_problems_first_then_upcoming_then_sent_newest_first():
    lead = LeadFactory()
    now = timezone.now()
    sent_old = _tp(lead, name="Old", status=TouchPoint.Status.SENT, sent_at=now - timedelta(days=2))
    sent_new = _tp(lead, name="New", status=TouchPoint.Status.SENT, sent_at=now - timedelta(days=1))
    _tp(lead, name="Soon", scheduled_for=now + timedelta(hours=1))
    _tp(lead, name="Later", scheduled_for=now + timedelta(days=1))
    failed = _tp(lead, name="Failed one", status=TouchPoint.Status.FAILED)
    for tp in (sent_old, sent_new):
        DeliveryFactory(touchpoint=tp, message=tp.message, status=Delivery.Status.SENT)
    DeliveryFactory(touchpoint=failed, message=failed.message, status=Delivery.Status.FAILED)

    names = [r.name for r in history.for_lead(lead)]

    assert names == ["Failed one", "Soon", "Later", "New", "Old"]


def test_held_quiet_hours_delivery_shows_the_reason_and_waiting():
    lead = LeadFactory()
    tp = _tp(lead, scheduled_for=timezone.now())
    DeliveryFactory(
        touchpoint=tp,
        message=tp.message,
        status=Delivery.Status.QUEUED,
        reason="Quiet hours until 8:00 AM PST",
        recipient_label="Dana Whitfield (lead passenger)",
        channel="sms",
    )

    (row,) = history.for_lead(lead)

    assert row.status == "Waiting"
    assert row.reason == "Quiet hours until 8:00 AM PST"
    assert row.to == "Dana Whitfield (lead passenger) · SMS"


def test_touchpoint_without_deliveries_carries_its_own_wording():
    lead = LeadFactory()
    _tp(lead, name="W", error="Waiting for the driver details")
    _tp(lead, name="S", scheduled_for=timezone.now() + timedelta(days=1))
    _tp(lead, name="O", status=TouchPoint.Status.CANCELLED, error="Switched off")
    _tp(lead, name="K", status=TouchPoint.Status.SKIPPED)

    rows = {r.name: r for r in history.for_lead(lead)}

    assert (rows["W"].status, rows["W"].reason) == ("Waiting", "Waiting for the driver details")
    assert rows["S"].status == "Scheduled"
    assert (rows["O"].status, rows["O"].reason) == ("Cancelled", "Switched off")
    assert (rows["K"].status, rows["K"].reason) == ("Skipped", "No recipient")


def test_trip_rows_use_the_trip_zone_with_abbreviation_order_rows_use_settings_zone():
    lead = LeadFactory()
    trip = ReservationFactory(lead=lead, pickup_timezone="America/Los_Angeles")
    at = datetime(2026, 12, 1, 16, 30, tzinfo=UTC)
    _tp(lead, name="Trip", reservation=trip, scheduled_for=at)
    _tp(lead, name="Order", scheduled_for=at)

    rows = {r.name: r for r in history.for_lead(lead)}

    assert rows["Trip"].when_display == "Dec 1, 8:30 AM PST"
    assert rows["Order"].when_display.startswith("Dec 1, ")
    assert rows["Trip"].trip_label


def test_query_count_is_bounded_for_30_touchpoints(django_assert_max_num_queries):
    lead = LeadFactory()
    trip = ReservationFactory(lead=lead)
    for i in range(30):
        tp = _tp(lead, name=f"M{i}", reservation=trip, status=TouchPoint.Status.SENT)
        DeliveryFactory(touchpoint=tp, message=tp.message, status=Delivery.Status.SENT)

    with django_assert_max_num_queries(6):
        assert len(history.for_lead(lead)) == 30


def test_order_and_workspace_pages_show_the_history(logged_in_client):
    lead = LeadFactory(status="booked")
    tp = _tp(lead, name="Driver details")
    DeliveryFactory(
        touchpoint=tp,
        message=tp.message,
        status=Delivery.Status.QUEUED,
        reason="Quiet hours until 8:00 AM PST",
    )
    for name in ("order_detail", "lead_detail"):
        body = logged_in_client.get(reverse(name, args=[lead.pk])).content.decode()
        assert "Message history" in body
        assert "Quiet hours until 8:00 AM PST" in body


def test_pages_show_an_empty_state(logged_in_client):
    lead = LeadFactory(status="booked")
    body = logged_in_client.get(reverse("order_detail", args=[lead.pk])).content.decode()
    assert "No messages sent or scheduled" in body


def test_blank_trip_zone_still_shows_the_office_zone_abbreviation(settings):
    lead = LeadFactory()
    trip = ReservationFactory(lead=lead, pickup_timezone="")
    at = datetime(2026, 11, 22, 13, 0, tzinfo=UTC)
    _tp(lead, name="Trip", reservation=trip, scheduled_for=at)
    _tp(lead, name="Order", scheduled_for=at)

    rows = {r.name: r for r in history.for_lead(lead)}

    abbreviation = at.astimezone(ZoneInfo(settings.TIME_ZONE)).tzname()
    assert rows["Trip"].when_display.endswith(f" {abbreviation}")
    assert rows["Order"].when_display.endswith(f" {abbreviation}")


def test_cancelled_sends_sort_after_sent_ones_newest_cancelled_first():
    lead = LeadFactory()
    now = timezone.now()
    sent = _tp(lead, name="Sent", status=TouchPoint.Status.SENT, sent_at=now - timedelta(days=9))
    DeliveryFactory(touchpoint=sent, message=sent.message, status=Delivery.Status.SENT)
    first = _tp(
        lead, name="Cancelled early", status=TouchPoint.Status.CANCELLED,
        scheduled_for=now + timedelta(days=30),
    )  # fmt: skip
    second = _tp(
        lead, name="Cancelled late", status=TouchPoint.Status.CANCELLED,
        scheduled_for=now + timedelta(days=1),
    )  # fmt: skip
    TouchPoint.objects.filter(pk=first.pk).update(updated_at=now - timedelta(hours=5))
    TouchPoint.objects.filter(pk=second.pk).update(updated_at=now - timedelta(hours=1))

    names = [r.name for r in history.for_lead(lead)]

    assert names == ["Sent", "Cancelled late", "Cancelled early"]


def test_failed_and_skipped_deliveries_are_dated_when_they_happened():
    lead = LeadFactory()
    due = datetime(2026, 12, 1, 16, 0, tzinfo=UTC)
    happened = datetime(2026, 12, 1, 16, 5, tzinfo=UTC)
    for name, status in (("Bad", Delivery.Status.FAILED), ("Skip", Delivery.Status.SKIPPED)):
        tp = _tp(lead, name=name, scheduled_for=due)
        d = DeliveryFactory(touchpoint=tp, message=tp.message, status=status)
        Delivery.objects.filter(pk=d.pk).update(updated_at=happened)

    rows = history.for_lead(lead)

    assert {r.when for r in rows} == {happened}


def test_unknown_delivery_status_does_not_break_the_history():
    lead = LeadFactory()
    tp = _tp(lead, name="Odd")
    d = DeliveryFactory(touchpoint=tp, message=tp.message, status=Delivery.Status.SENT)
    Delivery.objects.filter(pk=d.pk).update(status="mystery")

    (row,) = history.for_lead(lead)

    assert row.status == "mystery"


def test_history_is_escaped(logged_in_client):
    lead = LeadFactory(status="booked")
    tp = _tp(lead, name="Driver details")
    DeliveryFactory(
        touchpoint=tp,
        message=tp.message,
        status=Delivery.Status.FAILED,
        reason="<script>alert(1)</script>",
        recipient_label="<b>Eve</b>",
    )
    body = logged_in_client.get(reverse("order_detail", args=[lead.pk])).content.decode()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<b>Eve</b>" not in body
    assert "&lt;b&gt;Eve&lt;/b&gt;" in body


@pytest.mark.parametrize("name", ["order_detail", "lead_detail"])
def test_anonymous_is_redirected_from_the_history_pages(client, name):
    lead = LeadFactory(status="booked")
    response = client.get(reverse(name, args=[lead.pk]))
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_an_old_kind_with_no_message_shows_its_kind():
    """A TouchPoint from before the rows (no message) whose kind no row carries any more:
    the history and the admin name it by its kind rather than crash."""
    lead = LeadFactory()
    tp = TouchPointFactory(lead=lead, message=None, kind="greeting")

    (row,) = history.for_lead(lead)

    assert row.name == "greeting"
    assert str(tp) == "greeting · Scheduled"


def test_the_delivery_admin_reads_each_touchpoint_with_its_row():
    from django.contrib import admin

    from apps.messaging.models import Delivery as D

    related = admin.site._registry[D].list_select_related
    assert any(path.split("__")[0] == "touchpoint" for path in related)


@pytest.mark.parametrize(
    "reason,problem",
    [
        (sender.NO_RECIPIENT, True),
        (sender.MISSING["sms"], True),
        (sender.MISSING["email"], True),
        ("Already sent for Nov 14", False),
        ("day already confirmed", False),
        (rules.COVERED, False),
        (sender.OUTBOUND_OFF, False),
        ("Switched off", False),
    ],
)
def test_only_a_skip_someone_should_fix_is_a_problem(reason, problem):
    lead = LeadFactory()
    now = timezone.now()
    sent = _tp(lead, name="Sent", status=TouchPoint.Status.SENT, sent_at=now - timedelta(days=1))
    DeliveryFactory(touchpoint=sent, message=sent.message, status=Delivery.Status.SENT)
    _tp(lead, name="Soon", scheduled_for=now + timedelta(hours=1))
    skipped = _tp(lead, name="Skipped", status=TouchPoint.Status.SKIPPED, error=reason)
    DeliveryFactory(
        touchpoint=skipped, message=skipped.message, status=Delivery.Status.SKIPPED, reason=reason
    )
    _tp(lead, name="Bare skip", status=TouchPoint.Status.SKIPPED, error=reason)

    names = [r.name for r in history.for_lead(lead)]

    if problem:
        assert names[:2] == ["Skipped", "Bare skip"] and names[2] == "Soon"
    else:
        assert names[0] == "Soon" and set(names[1:]) == {"Sent", "Skipped", "Bare skip"}


# --- PAY-05 / LEAD-15: a Podium refusal reads as a sentence, not the client's raw error ---

_RAW = (
    '400 POST https://api.podium.com/v4/messages → {"code":"bad_request",'
    '"message":"Contact opted out"}'
)


def test_a_failed_delivery_shows_podiums_reason_not_its_json():
    lead = LeadFactory()
    tp = _tp(lead, status=TouchPoint.Status.FAILED)
    DeliveryFactory(touchpoint=tp, message=tp.message, status=Delivery.Status.FAILED, reason=_RAW)

    (row,) = history.for_lead(lead)

    assert row.reason == "Podium refused the message: Contact opted out"


def test_a_failed_touchpoint_with_no_delivery_reads_cleanly_too():
    lead = LeadFactory()
    _tp(lead, status=TouchPoint.Status.FAILED, error=_RAW)

    (row,) = history.for_lead(lead)

    assert row.reason == "Podium refused the message: Contact opted out"
