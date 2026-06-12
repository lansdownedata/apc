from datetime import timedelta

import pytest
from django.utils import timezone

from apps.leads.factories import LeadFactory
from apps.messaging.factories import MessageFactory, ReviewFactory, TouchPointFactory
from apps.messaging.models import Message, Review, TouchPoint

pytestmark = pytest.mark.django_db


# --- Message ---------------------------------------------------------------
def test_message_is_inbound():
    assert MessageFactory(direction=Message.Direction.IN).is_inbound is True
    assert MessageFactory(direction=Message.Direction.OUT).is_inbound is False


def test_thread_is_chronological():
    lead = LeadFactory()
    first = MessageFactory(lead=lead)
    second = MessageFactory(lead=lead)
    assert list(lead.messages.all()) == [first, second]


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
