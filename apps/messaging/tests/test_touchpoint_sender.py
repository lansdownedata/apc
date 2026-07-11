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
from apps.messaging.models import TouchPoint
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
    assert calls == {("email", "a@example.com"), ("sms", "5551234567")}


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


def test_booked_lead_skips_tp6_but_not_review_request(settings):
    settings.TOUCHPOINTS_ENABLED = True
    lead = LeadFactory(status=Lead.Status.BOOKED)
    tp6 = _due_tp(lead=lead, kind=TouchPoint.Kind.TP6_QUOTE_FOLLOWUP)
    review = _due_tp(lead=lead, kind=TouchPoint.Kind.REVIEW_REQUEST)

    with patch("apps.messaging.touchpoints.podium.send_message") as mock_send:
        result = touchpoints.run_touchpoints()

    assert result == 0
    tp6.refresh_from_db()
    review.refresh_from_db()
    assert tp6.status == TouchPoint.Status.SKIPPED
    assert "booked" in tp6.error.lower()
    # review_request is not the booked-skip path; it's skipped for its own "not wired" reason.
    assert review.status == TouchPoint.Status.SKIPPED
    assert "not wired" in review.error.lower()
    mock_send.assert_not_called()


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
