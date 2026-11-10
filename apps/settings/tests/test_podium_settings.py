"""Settings → Integrations → Podium: connect, see the connection, check the webhook."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import requests
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.integrations import services
from apps.integrations.factories import PodiumCredentialFactory
from apps.integrations.podium import PodiumAPIError, podium_webhooks
from apps.settings.menu import current_item
from apps.settings.views import split_webhooks

pytestmark = pytest.mark.django_db

LIST = "apps.integrations.podium.list_webhooks"


@pytest.fixture(autouse=True)
def podium_keys(settings):
    """Podium configured unless a test blanks it; never read from .env."""
    settings.PODIUM_CLIENT_ID = "podium-client"
    settings.PODIUM_LOCATION_UID = ""
    settings.PUBLIC_BASE_URL = "https://x/"
    settings.DEBUG = False


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role="owner_admin"))
    return client


def _page(owner) -> str:
    resp = owner.get(reverse("podium_settings"))
    assert resp.status_code == 200
    return resp.content.decode()


def _check(owner):
    resp = owner.post(reverse("podium_check_webhook"))
    assert resp.status_code == 302
    assert resp["Location"] == reverse("podium_settings")
    return resp, [(m.level_tag, str(m)) for m in get_messages(resp.wsgi_request)]


# --- the page ------------------------------------------------------------------------------


def test_not_configured(owner, settings):
    settings.PODIUM_CLIENT_ID = ""

    html = _page(owner)

    assert "Podium isn't configured on this server." in html
    assert reverse("integrations:podium_authorize") not in html
    assert "Connect Podium" not in html


def test_not_connected_offers_connect(owner):
    html = _page(owner)

    assert "Connect Podium" in html
    assert f'href="{reverse("integrations:podium_authorize")}"' in html
    assert "Reconnect" not in html


def test_connected_shows_the_connection(owner):
    PodiumCredentialFactory(location_uid="loc-1", scopes="read_messages write_messages")

    html = _page(owner)

    assert "loc-1" in html
    assert "read_messages write_messages" in html
    assert "Reconnect" in html
    assert f'href="{reverse("integrations:podium_authorize")}"' in html
    assert f'action="{reverse("podium_check_webhook")}"' in html
    assert "Connect Podium" not in html


def test_location_falls_back_to_the_setting(owner, settings):
    settings.PODIUM_LOCATION_UID = "loc-from-env"
    PodiumCredentialFactory(location_uid="")

    assert "loc-from-env" in _page(owner)


def test_connected_since_uses_the_company_clock(owner, settings):
    settings.TIME_ZONE = "America/New_York"
    cred = PodiumCredentialFactory(location_uid="loc-1")
    cred.created_at = datetime(2026, 11, 8, 15, 27, tzinfo=UTC)
    cred.save(update_fields=["created_at"])

    assert "Sun Nov 8 · 10:27 AM" in _page(owner)


def test_page_makes_no_podium_call(owner):
    PodiumCredentialFactory(location_uid="loc-1")
    with patch(LIST) as listed:
        _page(owner)
    listed.assert_not_called()


def _debug_without_toolbar(settings) -> None:
    """DEBUG on, but the debug toolbar (dev-only, its URLs absent under test) kept out."""
    settings.DEBUG = True
    settings.INTERNAL_IPS = []


def test_ngrok_hint_only_in_debug(owner, settings):
    hint = "Connect from the public (ngrok) address."
    assert hint not in _page(owner)

    _debug_without_toolbar(settings)
    html = _page(owner)
    assert hint in html
    assert "Invalid OAuth state" in html


def test_ngrok_hint_shows_when_not_configured(owner, settings):
    _debug_without_toolbar(settings)
    settings.PODIUM_CLIENT_ID = ""

    assert "Connect from the public (ngrok) address." in _page(owner)


def test_agent_cannot_open_it(client):
    client.force_login(UserFactory())

    resp = client.get(reverse("podium_settings"))

    assert resp.status_code in (302, 403)


def test_agent_cannot_check_the_webhook(client):
    client.force_login(UserFactory())

    with patch(LIST) as listed:
        resp = client.post(reverse("podium_check_webhook"))

    assert resp.status_code in (302, 403)
    listed.assert_not_called()


def test_podium_is_current_in_the_menu():
    item = current_item("podium_settings")
    assert item is not None and item.label == "Podium"
    assert current_item("podium_check_webhook").label == "Podium"


# --- check webhook -------------------------------------------------------------------------


def _hook(url: str, *events: str) -> dict:
    return {"uid": url, "url": url, "eventTypes": list(events)}


def test_check_webhook_lists_ours(owner):
    PodiumCredentialFactory()
    body = {"data": [_hook("https://x/webhooks/podium/", "message.received", "message.sent")]}
    with patch(LIST, return_value=body):
        _, flashes = _check(owner)

    assert flashes == [("success", "Webhook registered: https://x/webhooks/podium/ (2 events)")]


def test_check_webhook_only_another_host_is_not_this_site(owner):
    """A stale hook on the dev tunnel must not pass for this site's own hook."""
    PodiumCredentialFactory()
    body = {"data": [_hook("https://tunnel.ngrok-free.dev/webhooks/podium/", "message.sent")]}
    with patch(LIST, return_value=body):
        _, flashes = _check(owner)

    assert flashes == [
        ("warning", "No Podium webhook is registered for this site (https://x/webhooks/podium/)."),
        (
            "warning",
            "Also registered, points elsewhere: https://tunnel.ngrok-free.dev/webhooks/podium/",
        ),
    ]


def test_check_webhook_ours_and_one_elsewhere(owner):
    PodiumCredentialFactory()
    body = {
        "data": [
            _hook("https://b/webhooks/podium/"),
            _hook("https://x/webhooks/podium/", "message.sent"),
            _hook("https://elsewhere/hook", "message.sent"),
        ]
    }
    with patch(LIST, return_value=body):
        _, flashes = _check(owner)

    assert flashes == [
        ("success", "Webhook registered: https://x/webhooks/podium/ (1 event)"),
        ("warning", "Also registered, points elsewhere: https://b/webhooks/podium/"),
    ]


def test_check_webhook_blank_base_url_uses_the_request_host(owner, settings):
    settings.PUBLIC_BASE_URL = ""
    PodiumCredentialFactory()
    body = {"data": [_hook("http://testserver/webhooks/podium/", "message.sent")]}
    with patch(LIST, return_value=body):
        _, flashes = _check(owner)

    assert flashes == [
        ("success", "Webhook registered: http://testserver/webhooks/podium/ (1 event)")
    ]


def test_check_webhook_none_registered(owner):
    PodiumCredentialFactory()
    with patch(LIST, return_value={"data": []}):
        _, flashes = _check(owner)

    assert flashes == [
        ("warning", "No Podium webhook is registered for this site (https://x/webhooks/podium/).")
    ]


# --- split_webhooks ------------------------------------------------------------------------


def test_split_webhooks_normalises_scheme_host_and_trailing_slash():
    hooks = [
        _hook("HTTPS://X.example.com/webhooks/podium"),
        _hook("https://x.example.com/webhooks/podium/"),
        _hook("http://x.example.com/webhooks/podium/"),
        _hook("https://y.example.com/webhooks/podium/"),
    ]

    ours, elsewhere = split_webhooks(hooks, "https://x.example.com/webhooks/podium/")

    assert [h["url"] for h in ours] == [
        "HTTPS://X.example.com/webhooks/podium",
        "https://x.example.com/webhooks/podium/",
    ]
    assert [h["url"] for h in elsewhere] == [
        "http://x.example.com/webhooks/podium/",
        "https://y.example.com/webhooks/podium/",
    ]


def test_split_webhooks_path_case_still_matters():
    ours, elsewhere = split_webhooks(
        [_hook("https://x/Webhooks/Podium/")], "https://x/webhooks/podium/"
    )

    assert ours == []
    assert len(elsewhere) == 1


def test_check_webhook_api_error_is_readable(owner):
    PodiumCredentialFactory()
    error = PodiumAPIError('401 GET https://api.podium.com/v4/webhooks → {"message":"bad token"}')
    with patch(LIST, side_effect=error):
        _, flashes = _check(owner)

    [(level, text)] = flashes
    assert level == "error"
    assert "bad token" in text
    assert "https://api.podium.com" not in text


def test_check_webhook_unreachable_is_a_flash_not_a_500(owner):
    PodiumCredentialFactory()
    with patch(LIST, side_effect=requests.ConnectionError()):
        _, flashes = _check(owner)

    assert flashes == [("error", "Could not reach Podium. Try again in a minute.")]


def test_check_webhook_timeout_is_a_flash(owner):
    PodiumCredentialFactory()
    with patch(LIST, side_effect=requests.Timeout()):
        _, flashes = _check(owner)

    assert flashes == [("error", "Could not reach Podium. Try again in a minute.")]


def test_check_webhook_not_connected(owner):
    _, flashes = _check(owner)

    assert flashes == [("error", "Podium is not connected.")]


def test_check_webhook_refresh_refused_asks_for_reconnect(owner):
    """An expired token is renewed first; Podium refusing that is a flash, not a 500."""
    PodiumCredentialFactory(expires_at=timezone.now() - timedelta(minutes=1))
    refused = requests.Response()
    refused.status_code = 400
    with patch.object(services.requests, "post", side_effect=requests.HTTPError(response=refused)):
        _, flashes = _check(owner)

    assert flashes == [("error", "Podium would not renew the connection. Reconnect Podium.")]


def test_check_webhook_refresh_unreachable(owner):
    PodiumCredentialFactory(expires_at=timezone.now() - timedelta(minutes=1))
    with patch.object(services.requests, "post", side_effect=requests.ConnectionError()):
        _, flashes = _check(owner)

    assert flashes == [("error", "Could not reach Podium. Try again in a minute.")]


def test_get_check_webhook_is_405(owner):
    assert owner.get(reverse("podium_check_webhook")).status_code == 405


# --- podium_webhooks -----------------------------------------------------------------------


def test_podium_webhooks_keeps_only_ours():
    body = {
        "data": [
            {"uid": "w1", "url": "https://x/webhooks/podium/", "eventTypes": ["a"]},
            {"uid": "w2", "url": "https://x/webhooks/stripe/", "eventTypes": ["a"]},
            {"uid": "w3", "eventTypes": ["a"]},
        ]
    }
    with patch(LIST, return_value=body):
        assert [w["uid"] for w in podium_webhooks()] == ["w1"]


def test_podium_webhooks_empty_body():
    with patch(LIST, return_value={}):
        assert podium_webhooks() == []
