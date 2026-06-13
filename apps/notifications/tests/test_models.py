import pytest

from apps.leads.factories import LeadFactory
from apps.notifications.factories import NotificationFactory
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def test_notify_creates_unread():
    lead = LeadFactory()
    note = Notification.notify(
        lead, Notification.Kind.BALANCE_FAILED, title="Balance charge failed", detail="declined"
    )
    assert note.pk is not None
    assert note.read is False
    assert note.lead == lead
    assert note.kind == Notification.Kind.BALANCE_FAILED
    assert note.detail == "declined"


def test_unread_queryset_filters_read():
    NotificationFactory(read=False)
    NotificationFactory(read=True)
    assert Notification.objects.unread().count() == 1


def test_mark_read():
    note = NotificationFactory(read=False)
    note.mark_read()
    note.refresh_from_db()
    assert note.read is True


def test_str_includes_title():
    assert "Balance charge failed" in str(NotificationFactory(title="Balance charge failed"))
