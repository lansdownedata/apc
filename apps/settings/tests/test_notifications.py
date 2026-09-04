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


def test_toggling_a_message_persists(client):
    client.force_login(_owner_admin())
    resp = client.post(
        reverse("notifications"),
        {
            "enabled": "on",
            "wedding_final_details_enabled": "on",
            "trip_confirm_customer_enabled": "on",
            "trip_confirm_affiliate_enabled": "on",
            "driver_released_enabled": "on",
            "status_dispatched_enabled": "on",
            # on_the_way / arrived left off
        },
    )
    assert resp.status_code == 302
    cfg = NotificationConfig.load()
    assert cfg.status_dispatched_enabled is True
    assert cfg.status_on_the_way_enabled is False


def test_non_owner_admin_is_blocked(client):
    client.force_login(UserFactory())
    resp = client.get(reverse("notifications"))
    assert resp.status_code in (302, 403)


def test_index_tile_links_to_the_screen(client):
    client.force_login(_owner_admin())
    resp = client.get(reverse("settings_index"))
    assert reverse("notifications").encode() in resp.content
