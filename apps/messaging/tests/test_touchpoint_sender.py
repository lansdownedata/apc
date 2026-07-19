from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.contacts.factories import ContactFactory
from apps.integrations.podium import PodiumAPIError
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.messaging import touchpoints
from apps.messaging.factories import TouchPointFactory
from apps.messaging.models import Review, TouchPoint
from apps.payments.factories import PaymentPlanFactory
from apps.payments.models import PaymentPlan

pytestmark = pytest.mark.django_db


def _due_tp(**kwargs):
    kwargs.setdefault("scheduled_for", timezone.now() - timedelta(minutes=1))
    return TouchPointFactory(**kwargs)


def test_disabled_flag_returns_zero_and_leaves_rows_untouched(settings):
    settings.TOUCHPOINTS_ENABLED = False
    tp = _due_tp()

    result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED


def test_tp1_sends_on_both_channels_and_marks_sent(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(email="a@example.com", phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)

    with patch(
        "apps.messaging.touchpoints.podium.send_message",
        return_value={"uid": "msg-1"},
    ) as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 1
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    assert tp.sent_at is not None
    assert tp.podium_message_uid == "msg-1"
    assert mock_send.call_count == 2
    calls = {(c.kwargs["channel_type"], c.kwargs["identifier"]) for c in mock_send.call_args_list}
    # Podium's own name for the SMS channel is "phone", not "sms" — see _PODIUM_CHANNEL.
    assert calls == {("email", "a@example.com"), ("phone", "5551234567")}


def test_sms_touchpoint_uses_podiums_phone_channel_type(settings):
    """Regression: touchpoints.py used to pass channel_type="sms" straight through from
    TouchPointTemplate.channels, but Podium's API expects "phone" — every SMS touch-point
    was being rejected. apps/messaging/views.py already did this translation; touchpoints.py
    now shares the same mapping."""
    settings.TOUCHPOINTS_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example"
    contact = ContactFactory(email="", phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)

    with patch(
        "apps.messaging.touchpoints.podium.send_message",
        return_value={"uid": "msg-1"},
    ) as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 1
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["channel_type"] == "phone"
    assert mock_send.call_args.kwargs["identifier"] == "5551234567"


def test_review_invite_sms_uses_podiums_phone_channel_type(settings):
    """Same bug, second call site: _send_review_invite hardcoded channel_type="sms"."""
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(name="Jane Doe", phone="5551234567")
    lead = LeadFactory(contact=contact)
    _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with (
        patch(
            "apps.messaging.touchpoints.podium.send_message", return_value={"uid": "msg-1"}
        ) as mock_send,
        patch(
            "apps.messaging.touchpoints.podium.create_review_invitation",
            return_value={"uid": "inv-1", "url": "https://podium.example/r/inv-1"},
        ),
    ):
        touchpoints.run_touchpoints()

    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["channel_type"] == "phone"


def test_lost_lead_is_skipped(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(status=Lead.Status.LOST)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)

    with patch("apps.messaging.touchpoints.podium.send_message") as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    assert "lost" in tp.error.lower()
    mock_send.assert_not_called()


def test_booked_lead_skips_tp6_but_sends_review_request(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(name="Jane Doe", phone="5551234567")
    lead = LeadFactory(status=Lead.Status.BOOKED, contact=contact)
    tp6 = _due_tp(lead=lead, kind=TouchPoint.Kind.TP6_QUOTE_FOLLOWUP)
    review = _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with (
        patch(
            "apps.messaging.touchpoints.podium.send_message", return_value={"uid": "msg-1"}
        ) as mock_send,
        patch(
            "apps.messaging.touchpoints.podium.create_review_invitation",
            return_value={"uid": "inv-1", "url": "https://podium.example/r/inv-1"},
        ) as mock_invite,
    ):
        result = touchpoints.run_touchpoints()

    assert result == 1
    tp6.refresh_from_db()
    review.refresh_from_db()
    assert tp6.status == TouchPoint.Status.SKIPPED
    assert "booked" in tp6.error.lower()
    # review_request is NOT skipped on BOOKED — it's the whole point of the touch-point.
    assert review.status == TouchPoint.Status.SENT
    mock_invite.assert_called_once_with(phone="5551234567", first_name="Jane", last_name="Doe")
    mock_send.assert_called_once()
    assert "https://podium.example/r/inv-1" in mock_send.call_args.kwargs["body"]


def test_lost_lead_skips_review_request(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(status=Lead.Status.LOST)
    review = _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with patch("apps.messaging.touchpoints.podium.create_review_invitation") as mock_invite:
        result = touchpoints.run_touchpoints()

    assert result == 0
    review.refresh_from_db()
    assert review.status == TouchPoint.Status.SKIPPED
    assert "lost" in review.error.lower()
    mock_invite.assert_not_called()
    assert not Review.objects.filter(lead=lead).exists()


def test_review_request_creates_review_row_with_invite_uid(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(name="Jane Doe", phone="5551234567")
    lead = LeadFactory(contact=contact)
    _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with (
        patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "msg-1"}),
        patch(
            "apps.messaging.touchpoints.podium.create_review_invitation",
            return_value={"uid": "inv-42", "url": "https://podium.example/r/inv-42"},
        ),
    ):
        touchpoints.run_touchpoints()

    review = Review.objects.get(lead=lead)
    assert review.contact == contact
    assert review.podium_review_invite_uid == "inv-42"
    assert review.delivery_status == Review.DeliveryStatus.SENT
    assert review.requested_at is not None


def test_review_request_no_phone_is_skipped(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(phone="", email="jane@example.com")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with patch("apps.messaging.touchpoints.podium.create_review_invitation") as mock_invite:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    assert "phone" in tp.error.lower()
    mock_invite.assert_not_called()
    assert not Review.objects.filter(lead=lead).exists()


def test_review_request_invite_failure_marks_failed_no_review_row(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with patch(
        "apps.messaging.touchpoints.podium.create_review_invitation",
        side_effect=PodiumAPIError("500 boom"),
    ):
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.FAILED
    assert "boom" in tp.error
    assert not Review.objects.filter(lead=lead).exists()


def test_deposit_paid_skips_quote_kinds(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory()
    PaymentPlanFactory(lead=lead, deposit_status=PaymentPlan.DepositStatus.PAID)
    tp3 = _due_tp(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)

    with patch("apps.messaging.touchpoints.podium.send_message") as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp3.refresh_from_db()
    assert tp3.status == TouchPoint.Status.SKIPPED
    assert "deposit" in tp3.error.lower()
    mock_send.assert_not_called()


def test_contact_with_no_channels_is_skipped(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(email="", phone="")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)

    with patch("apps.messaging.touchpoints.podium.send_message") as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SKIPPED
    mock_send.assert_not_called()


def test_all_channels_failing_marks_failed_with_error(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact = ContactFactory(email="a@example.com", phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)

    with patch(
        "apps.messaging.touchpoints.podium.send_message",
        side_effect=PodiumAPIError("500 boom"),
    ):
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.FAILED
    assert "boom" in tp.error


def test_not_due_rows_are_untouched(settings):
    settings.TOUCHPOINTS_ENABLED = True
    tp = TouchPointFactory(
        kind=TouchPoint.Kind.TP1_WELCOME, scheduled_for=timezone.now() + timedelta(hours=1)
    )

    with patch("apps.messaging.touchpoints.podium.send_message") as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SCHEDULED
    mock_send.assert_not_called()


def test_quote_link_uses_public_base_url(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.PUBLIC_BASE_URL = "https://apc.example"
    contact = ContactFactory(email="a@example.com", phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp = _due_tp(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)

    with patch(
        "apps.messaging.touchpoints.podium.send_message",
        return_value={"uid": "msg-1"},
    ) as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 1
    tp.refresh_from_db()
    assert tp.status == TouchPoint.Status.SENT
    sms_call = next(c for c in mock_send.call_args_list if c.kwargs["channel_type"] == "phone")
    assert "https://apc.example/quote/" in sms_call.kwargs["body"]


def test_blank_public_base_url_leaves_quote_link_row_scheduled_but_still_sends_tp1(settings):
    settings.TOUCHPOINTS_ENABLED = True
    settings.PUBLIC_BASE_URL = ""
    contact = ContactFactory(email="a@example.com", phone="5551234567")
    lead = LeadFactory(contact=contact)
    tp3 = _due_tp(lead=lead, kind=TouchPoint.Kind.TP3_QUOTE_SENT_SMS)
    tp1 = _due_tp(lead=lead, kind=TouchPoint.Kind.TP1_WELCOME)

    with patch(
        "apps.messaging.touchpoints.podium.send_message",
        return_value={"uid": "msg-1"},
    ) as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 1
    tp3.refresh_from_db()
    tp1.refresh_from_db()
    assert tp3.status == TouchPoint.Status.SCHEDULED
    assert tp1.status == TouchPoint.Status.SENT
    calls = {(c.kwargs["channel_type"], c.kwargs["identifier"]) for c in mock_send.call_args_list}
    assert calls == {("email", "a@example.com"), ("phone", "5551234567")}


def test_run_touchpoints_returns_sent_count(settings):
    settings.TOUCHPOINTS_ENABLED = True
    contact1 = ContactFactory(email="a@example.com", phone="5551234567")
    contact2 = ContactFactory(email="b@example.com", phone="5559876543")
    lead1 = LeadFactory(contact=contact1)
    lead2 = LeadFactory(contact=contact2)
    _due_tp(lead=lead1, kind=TouchPoint.Kind.TP1_WELCOME)
    _due_tp(lead=lead2, kind=TouchPoint.Kind.TP2_LEAD_FOLLOWUP)

    with patch("apps.messaging.touchpoints.podium.send_message", return_value={"uid": "x"}):
        result = touchpoints.run_touchpoints()

    assert result == 2
