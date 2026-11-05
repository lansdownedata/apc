from datetime import timedelta

import pytest
from django.utils import timezone

from apps.messaging.factories import DeliveryFactory, MessageRowFactory, TouchPointFactory
from apps.messaging.models import MessageTemplate, TouchPoint

pytestmark = pytest.mark.django_db


def test_offset_is_negative_for_before():
    row = MessageRowFactory(timing_amount=3, timing_unit="hours", timing_direction="before")
    assert row.offset == -timedelta(hours=3)


def test_offset_is_positive_for_after():
    row = MessageRowFactory(timing_amount=2, timing_unit="days", timing_direction="after")
    assert row.offset == timedelta(days=2)


def test_channels_in_sms_email_order():
    assert MessageRowFactory(send_sms=True, send_email=True).channels == ("sms", "email")
    assert MessageRowFactory(send_sms=False, send_email=True).channels == ("email",)
    assert MessageRowFactory(send_sms=True, send_email=False).channels == ("sms",)


def test_live_excludes_archived():
    live = MessageRowFactory()
    MessageRowFactory(archived_at=timezone.now())
    assert list(MessageTemplate.objects.live()) == [live]


def test_touchpoint_saves_key_longer_than_30_chars():
    key = "custom-" + "x" * 30
    tp = TouchPointFactory(kind=key)
    tp.refresh_from_db()
    assert tp.kind == key


def test_touchpoint_str_uses_message_name():
    row = MessageRowFactory(name="Hello there")
    tp = TouchPointFactory(kind="custom-1", message=row)
    assert "Hello there" in str(tp)


def test_delivery_links_touchpoint_and_message():
    delivery = DeliveryFactory()
    assert delivery.touchpoint.deliveries.get() == delivery
    assert delivery.message.pk
    assert delivery.status == "queued"


def test_touchpoint_message_is_protected():
    row = MessageRowFactory()
    TouchPointFactory(kind=TouchPoint.Kind.TP1_WELCOME, message=row)
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        row.delete()
