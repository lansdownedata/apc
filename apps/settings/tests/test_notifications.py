import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.messaging.models import NotificationConfig

pytestmark = pytest.mark.django_db


def _owner_admin():
    return UserFactory(role=UserFactory._meta.model.Role.OWNER_ADMIN)


def test_screen_loads(client):
    client.force_login(_owner_admin())
    resp = client.get(reverse("notifications"))
    assert resp.status_code == 200


def test_master_switch_persists(client):
    client.force_login(_owner_admin())
    resp = client.post(reverse("notifications"), {"csrfmiddlewaretoken": "x"})
    assert resp.status_code == 302
    assert NotificationConfig.load().enabled is False

    client.post(reverse("notifications"), {"enabled": "on"})
    assert NotificationConfig.load().enabled is True


def test_non_owner_admin_is_blocked(client):
    client.force_login(UserFactory())
    resp = client.get(reverse("notifications"))
    assert resp.status_code in (302, 403)


def test_index_tile_links_to_the_screen(client):
    client.force_login(_owner_admin())
    resp = client.get(reverse("settings_index"))
    assert reverse("notifications").encode() in resp.content
