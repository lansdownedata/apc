"""Staff can see whether the portal is live (APC-90).

While the go-live switch is off every staff page says so, once, in the shell — an agent
who presses Send should already know nothing will go. Settings shows the state too, and
says it is not changed there: it is an environment setting, flipped at go-live.
"""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User

pytestmark = pytest.mark.django_db

BANNER = "Outbound messaging is off"


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))


def test_every_staff_page_says_so_while_the_switch_is_off(client, owner, settings):
    settings.OUTBOUND_ENABLED = False
    for name in ("dashboard", "inbox", "dispatch_board"):
        assert BANNER in client.get(reverse(name)).content.decode(), name


def test_no_banner_once_live(client, owner):
    assert BANNER not in client.get(reverse("dashboard")).content.decode()


def test_settings_shows_not_live_while_the_switch_is_off(client, owner, settings):
    settings.OUTBOUND_ENABLED = False
    html = client.get(reverse("settings_index")).content.decode()
    assert "Go-live switch" in html
    assert "Not live" in html


def test_settings_shows_live_once_the_switch_is_on(client, owner):
    html = client.get(reverse("settings_index")).content.decode()
    assert "Go-live switch" in html
    assert "Not live" not in html
    assert "Live" in html
