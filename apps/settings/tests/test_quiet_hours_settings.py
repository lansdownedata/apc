from datetime import time

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.messaging.models import NotificationConfig

pytestmark = pytest.mark.django_db


def _admin(client):
    client.force_login(UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN))


def _page(client) -> str:
    return client.get(reverse("messages")).content.decode()


def test_defaults_are_9pm_to_8am():
    config = NotificationConfig.load()
    assert (config.quiet_start, config.quiet_end) == (time(21, 0), time(8, 0))


def test_header_shows_the_saved_window(client):
    _admin(client)
    assert "Quiet hours 9 PM to 8 AM" in _page(client)
    NotificationConfig.objects.filter(pk=1).update(quiet_start=time(22, 30), quiet_end=time(7, 0))
    assert "Quiet hours 10:30 PM to 7 AM" in _page(client)


def test_admin_saves(client):
    _admin(client)
    resp = client.post(reverse("messages_quiet_hours"), {"start": "22:00", "end": "07:30"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["label"] == "Quiet hours 10 PM to 7:30 AM"
    config = NotificationConfig.load()
    assert (config.quiet_start, config.quiet_end) == (time(22, 0), time(7, 30))


def test_start_equal_to_end_means_off(client):
    _admin(client)
    resp = client.post(reverse("messages_quiet_hours"), {"start": "08:00", "end": "08:00"})
    assert resp.status_code == 200
    assert resp.json()["label"] == "Quiet hours off"
    assert "Quiet hours off" in _page(client)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"start": "21:00"},
        {"start": "", "end": "08:00"},
        {"start": "25:00", "end": "08:00"},
        {"start": "nine", "end": "08:00"},
    ],
)
def test_bad_input_is_rejected_and_nothing_changes(client, data):
    _admin(client)
    resp = client.post(reverse("messages_quiet_hours"), data)
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert resp.json()["error"]
    config = NotificationConfig.load()
    assert (config.quiet_start, config.quiet_end) == (time(21, 0), time(8, 0))


def test_agent_is_forbidden(client):
    client.force_login(UserFactory())
    assert (
        client.post(reverse("messages_quiet_hours"), {"start": "22:00", "end": "07:00"}).status_code
        == 403
    )
    assert NotificationConfig.load().quiet_start == time(21, 0)


def test_get_is_not_allowed(client):
    _admin(client)
    assert client.get(reverse("messages_quiet_hours")).status_code == 405
