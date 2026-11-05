from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.messaging import history
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
