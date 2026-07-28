from datetime import timedelta

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.messaging.factories import (
    ConversationFactory,
    MessageFactory,
    ReviewFactory,
    TouchPointFactory,
)
from apps.messaging.models import Message, Review, TouchPoint

pytestmark = pytest.mark.django_db


# --- Message ---------------------------------------------------------------
def test_message_is_inbound():
    assert MessageFactory(direction=Message.Direction.IN).is_inbound is True
    assert MessageFactory(direction=Message.Direction.OUT).is_inbound is False


def test_thread_is_chronological():
    convo = ConversationFactory()
    first = MessageFactory(conversation=convo)
    second = MessageFactory(conversation=convo)
    assert list(convo.messages.all()) == [first, second]


# --- TouchPoint ------------------------------------------------------------
def test_touchpoint_is_due_when_scheduled_in_past():
    tp = TouchPointFactory(
        status=TouchPoint.Status.SCHEDULED,
        scheduled_for=timezone.now() - timedelta(minutes=5),
    )
    assert tp.is_due is True


def test_touchpoint_not_due_when_future():
    tp = TouchPointFactory(
        status=TouchPoint.Status.SCHEDULED,
        scheduled_for=timezone.now() + timedelta(hours=1),
    )
    assert tp.is_due is False


def test_touchpoint_not_due_when_already_sent():
    tp = TouchPointFactory(
        status=TouchPoint.Status.SENT,
        scheduled_for=timezone.now() - timedelta(minutes=5),
    )
    assert tp.is_due is False


# --- Review ----------------------------------------------------------------
def test_review_defaults():
    review = ReviewFactory()
    assert review.delivery_status == Review.DeliveryStatus.PENDING
    assert review.link_clicked is False
    assert review.has_rating is False


def test_review_with_rating():
    review = ReviewFactory(rating=5, delivery_status=Review.DeliveryStatus.DELIVERED)
    assert review.has_rating is True


# --- New Touch-point taxonomy (TP1-8) ------------------------------------------
def test_touchpoint_new_taxonomy():
    """TP1-8 kinds are available in TouchPoint.Kind."""
    assert TouchPoint.Kind.TP1_WELCOME == "tp1_welcome"
    assert TouchPoint.Kind.TP2_LEAD_FOLLOWUP == "tp2_lead_followup"
    assert TouchPoint.Kind.TP3_QUOTE_SENT_SMS == "tp3_quote_sent_sms"
    assert TouchPoint.Kind.TP4_VIEWED_SMS == "tp4_viewed_sms"
    assert TouchPoint.Kind.TP5_VIEWED_EMAIL == "tp5_viewed_email"
    assert TouchPoint.Kind.TP6_QUOTE_FOLLOWUP == "tp6_quote_followup"
    assert TouchPoint.Kind.TP7_EXPIRING == "tp7_expiring"
    assert TouchPoint.Kind.TP8_EXPIRED == "tp8_expired"
    assert TouchPoint.Kind.REVIEW_REQUEST == "review_request"


def test_touchpoint_status_has_cancelled_and_failed():
    """TouchPoint.Status now has CANCELLED and FAILED."""
    assert TouchPoint.Status.CANCELLED == "cancelled"
    assert TouchPoint.Status.FAILED == "failed"


def test_touchpoint_error_field():
    """TouchPoint has an error field for failure details."""
    tp = TouchPointFactory(error="Email service timeout")
    assert tp.error == "Email service timeout"


def test_message_read_at_defaults_null():
    """Message.read_at defaults to None (unread)."""
    msg = Message.objects.create(conversation=ConversationFactory(), direction=Message.Direction.IN)
    assert msg.read_at is None


# --- Lead quote lifecycle fields -----------------------------------------------
def test_lead_quote_lifecycle_fields():
    """Lead has quote_sent_at, quote_viewed_at, and quote_expires_at."""
    now = timezone.now()
    lead = LeadFactory(
        quote_sent_at=now,
        quote_viewed_at=now + timedelta(hours=1),
        quote_expires_at=now + timedelta(days=14),
    )
    assert lead.quote_sent_at == now
    assert lead.quote_viewed_at == now + timedelta(hours=1)
    assert lead.quote_expires_at == now + timedelta(days=14)


def test_lead_quote_expired_property_true_when_past():
    """Lead.quote_expired is True if quote_expires_at is in the past."""
    lead = LeadFactory(quote_expires_at=timezone.now() - timedelta(hours=1))
    assert lead.quote_expired is True


def test_lead_quote_expired_property_false_when_future():
    """Lead.quote_expired is False if quote_expires_at is in the future."""
    lead = LeadFactory(quote_expires_at=timezone.now() + timedelta(days=1))
    assert lead.quote_expired is False


def test_lead_quote_expired_property_false_when_null():
    """Lead.quote_expired is False if quote_expires_at is not set."""
    lead = LeadFactory(quote_expires_at=None)
    assert lead.quote_expired is False


def test_message_has_no_lead_field():
    """Messages belong to a conversation, never to a lead.

    A conversation may have zero leads (not qualified) or several (repeat customer),
    so there is no lead a message could sensibly point at.
    """
    field_names = {f.name for f in Message._meta.get_fields()}
    assert "lead" not in field_names
    assert "conversation" in field_names


def test_message_requires_a_conversation():
    from django.core.exceptions import ValidationError

    message = Message(direction=Message.Direction.IN, channel=Message.Channel.SMS, body="hi")
    with pytest.raises(ValidationError):
        message.full_clean()
