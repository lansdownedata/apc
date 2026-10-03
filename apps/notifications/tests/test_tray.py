"""APC-77 — the tray shows a person their own notifications plus the shared alerts, and
reading one doesn't hide a shared alert from anyone else."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.notifications.factories import NotificationFactory
from apps.notifications.models import Notification, NotificationRead

pytestmark = pytest.mark.django_db

_OVERDUE = Notification.Kind.TASK_OVERDUE


@pytest.fixture
def ana():
    return UserFactory()


@pytest.fixture
def ben():
    return UserFactory()


def _tray(client, user) -> list[str]:
    client.force_login(user)
    resp = client.get(reverse("dashboard"))
    return [n.title for n in resp.context["unread_notifications"]]


def _count(client, user) -> int:
    client.force_login(user)
    return client.get(reverse("dashboard")).context["unread_count"]


# --- scope -----------------------------------------------------------------------------


def test_a_person_sees_their_own_and_the_shared_alerts_only(client, ana, ben):
    NotificationFactory(user=ana, kind=_OVERDUE, title="Ana's task")
    NotificationFactory(user=ben, kind=_OVERDUE, title="Ben's task")
    NotificationFactory(title="Card declined")  # no user: shared

    assert sorted(_tray(client, ana)) == ["Ana's task", "Card declined"]
    assert sorted(_tray(client, ben)) == ["Ben's task", "Card declined"]


def test_an_escalation_to_three_admins_shows_once_each(client, ana, ben):
    cal = UserFactory()
    for user in (ana, ben, cal):
        NotificationFactory(user=user, kind=_OVERDUE, title="Escalated: Deposit received")

    for user in (ana, ben, cal):
        assert _tray(client, user) == ["Escalated: Deposit received"]
        assert _count(client, user) == 1


def test_the_dashboard_list_uses_the_same_scope(client, ana, ben):
    NotificationFactory(user=ben, kind=_OVERDUE, title="Ben's task")
    NotificationFactory(title="Card declined")
    client.force_login(ana)

    alerts = client.get(reverse("dashboard")).context["alerts"]

    assert [n.title for n in alerts] == ["Card declined"]


def test_the_badge_matches_the_list(client, ana, ben):
    NotificationFactory(user=ana, kind=_OVERDUE, title="Mine")
    NotificationFactory(user=ben, kind=_OVERDUE, title="Not mine")
    NotificationFactory(title="Shared")

    assert _count(client, ana) == len(_tray(client, ana)) == 2


# --- mark read -------------------------------------------------------------------------


def test_opening_one_marks_it_read_and_goes_to_its_page(client, ana):
    note = NotificationFactory(user=ana, kind=_OVERDUE, title="Mine")
    client.force_login(ana)

    resp = client.post(reverse("notification_read", args=[note.pk]))

    assert resp.status_code == 302
    assert resp.url == note.url
    note.refresh_from_db()
    assert note.read is True
    assert note.read_at is not None
    assert _tray(client, ana) == []


def test_a_shared_alert_read_by_one_person_stays_unread_for_the_other(client, ana, ben):
    note = NotificationFactory(title="Card declined")
    client.force_login(ana)

    client.post(reverse("notification_read", args=[note.pk]))
    client.post(reverse("notification_read", args=[note.pk]))  # twice is harmless

    note.refresh_from_db()
    assert note.read is False
    assert NotificationRead.objects.filter(notification=note, user=ana).count() == 1
    assert _tray(client, ana) == []
    assert _tray(client, ben) == ["Card declined"]


def test_mark_all_read_clears_my_tray_and_nobody_elses(client, ana, ben):
    NotificationFactory(user=ana, kind=_OVERDUE, title="Mine")
    NotificationFactory(user=ben, kind=_OVERDUE, title="Ben's task")
    NotificationFactory(title="Card declined")
    client.force_login(ana)

    resp = client.post(reverse("notifications_read_all"), HTTP_REFERER="/portal/leads/")

    assert resp.status_code == 302
    assert resp.url == "/portal/leads/"
    assert _count(client, ana) == 0
    assert sorted(_tray(client, ben)) == ["Ben's task", "Card declined"]


def test_mark_all_never_redirects_off_site(client, ana):
    client.force_login(ana)

    resp = client.post(reverse("notifications_read_all"), HTTP_REFERER="https://evil.example/x")

    assert resp.url == reverse("dashboard")


def test_someone_elses_notification_is_not_mine_to_read(client, ana, ben):
    note = NotificationFactory(user=ben, kind=_OVERDUE, title="Ben's task")
    client.force_login(ana)

    assert client.post(reverse("notification_read", args=[note.pk])).status_code == 404
    note.refresh_from_db()
    assert note.read is False


def test_reading_needs_a_post_and_a_login(client, ana):
    note = NotificationFactory(user=ana, kind=_OVERDUE)

    assert client.post(reverse("notification_read", args=[note.pk])).status_code == 302
    note.refresh_from_db()
    assert note.read is False
    client.force_login(ana)
    assert client.get(reverse("notification_read", args=[note.pk])).status_code == 405


# --- the tray markup -------------------------------------------------------------------


def test_the_tray_offers_mark_all_only_when_there_is_something_to_mark(client, ana):
    client.force_login(ana)
    assert "Mark all read" not in client.get(reverse("dashboard")).content.decode()

    NotificationFactory(title="Card declined")

    body = client.get(reverse("dashboard")).content.decode()
    assert "Mark all read" in body
    assert reverse("notifications_read_all") in body


# --- cost ------------------------------------------------------------------------------


def test_the_page_costs_the_same_however_many_notifications_there_are(client, ana, ben):
    # One order throughout: the dashboard's recent-leads list has its own per-lead cost,
    # which isn't what this measures.
    lead = LeadFactory()
    client.force_login(ana)
    NotificationFactory(lead=lead, title="one")
    with CaptureQueriesContext(connection) as few:
        client.get(reverse("dashboard"))
    for i in range(12):
        NotificationFactory(lead=lead, title=f"shared {i}")
        NotificationFactory(lead=lead, user=ana, kind=_OVERDUE, title=f"mine {i}")
        NotificationFactory(lead=lead, user=ben, kind=_OVERDUE, title=f"ben {i}")
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("dashboard"))

    assert len(many) == len(few)
