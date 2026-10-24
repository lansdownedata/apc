"""APC-41 — the QuickBooks settings screen: connect, callback, disconnect. HTTP mocked."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.integrations import qbo
from apps.integrations.models import QboCredential

pytestmark = pytest.mark.django_db

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
    "QBO_REDIRECT_URI": "https://apc.example.com/integrations/quickbooks/callback/",
}


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    return client


def _live():
    cred = QboCredential.load()
    cred.realm_id = "9130"
    cred.set_tokens(
        access="at",
        refresh="rt",
        access_expires_at=timezone.now() + timedelta(hours=1),
        refresh_expires_at=None,
    )
    cred.connected_at = timezone.now()
    cred.save()
    return cred


@override_settings(**QBO)
def test_the_settings_screen_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    assert client.get(reverse("quickbooks_settings")).status_code in (302, 403)


@override_settings(**QBO)
def test_the_screen_offers_connect_when_not_connected(owner):
    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "Connect QuickBooks" in html
    assert "Sandbox" in html


@override_settings(**QBO)
def test_the_screen_shows_the_company_and_who_connected_it(owner):
    cred = _live()
    cred.connected_by = UserFactory(first_name="Moe", last_name="A")
    cred.save()

    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "9130" in html and "Moe A" in html
    assert "Disconnect" in html and "Reconnect" in html
    assert cred.refresh_token_enc not in html and cred.access_token_enc not in html


@override_settings(**QBO)
def test_a_broken_connection_says_so_and_offers_reconnect(owner):
    cred = _live()
    cred.broken_at = timezone.now()
    cred.broken_reason = "QuickBooks refused to renew the connection. Reconnect it in Settings."
    cred.save()

    html = owner.get(reverse("quickbooks_settings")).content.decode()

    assert "refused to renew" in html and "Reconnect" in html


@override_settings(**QBO)
def test_connect_redirects_to_intuit_with_a_state_kept_in_the_session(owner):
    resp = owner.post(reverse("quickbooks_connect"))

    assert resp.status_code == 302
    assert resp["Location"].startswith("https://appcenter.intuit.com/connect/oauth2?")
    state = owner.session["qbo_oauth_state"]
    assert f"state={state}" in resp["Location"]


@override_settings(**QBO)
def test_the_callback_with_the_right_state_connects(owner):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo, "exchange_code") as exchange:
        exchange.return_value = _live()
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": "9130"},
        )

    assert resp.status_code == 302 and resp["Location"] == reverse("quickbooks_settings")
    assert exchange.call_args.args == ("c",)
    assert exchange.call_args.kwargs["realm_id"] == "9130"


@pytest.mark.parametrize("state", ["", "forged"])
@override_settings(**QBO)
def test_the_callback_refuses_a_missing_or_wrong_state(owner, state):
    owner.post(reverse("quickbooks_connect"))

    with patch.object(qbo, "exchange_code") as exchange:
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"code": "c", "state": state, "realmId": "9130"},
        )

    assert resp.status_code == 400
    exchange.assert_not_called()


@override_settings(**QBO)
def test_the_callback_reports_a_declined_consent(owner):
    owner.post(reverse("quickbooks_connect"))
    state = owner.session["qbo_oauth_state"]

    with patch.object(qbo, "exchange_code") as exchange:
        resp = owner.get(
            reverse("integrations:quickbooks_callback"),
            {"error": "access_denied", "state": state},
            follow=True,
        )

    from django.contrib.messages import get_messages

    exchange.assert_not_called()
    assert any("QuickBooks wasn't connected" in str(m) for m in get_messages(resp.wsgi_request))


@override_settings(**QBO)
def test_the_callback_is_owner_admin_only(client):
    client.force_login(UserFactory(role=User.Role.AGENT))

    resp = client.get(reverse("integrations:quickbooks_callback"), {"code": "c", "state": "s"})

    assert resp.status_code in (302, 403)


@override_settings(**QBO)
def test_disconnect_clears_the_connection(owner):
    _live()

    with patch.object(qbo.requests, "request"):
        resp = owner.post(reverse("quickbooks_disconnect"))

    assert resp.status_code == 302
    assert qbo.is_connected() is False


@override_settings(QBO_CLIENT_ID="", QBO_CLIENT_SECRET="")
def test_without_keys_the_tile_is_hidden_and_the_screen_404s(owner):
    index = owner.get(reverse("settings_index")).content.decode()

    assert "QuickBooks" not in index
    assert owner.get(reverse("quickbooks_settings")).status_code == 404


@override_settings(**QBO)
def test_with_keys_the_tile_shows_the_connection_state(owner):
    assert "Not connected" in owner.get(reverse("settings_index")).content.decode()
    _live()
    assert "Connected" in owner.get(reverse("settings_index")).content.decode()


@override_settings(**QBO)
def test_the_chrome_flag_follows_the_real_connection(owner):
    # Lazy, so pages that never read it pay no query.
    assert bool(owner.get(reverse("dashboard")).context["qbo_connected"]) is False
    _live()
    assert bool(owner.get(reverse("dashboard")).context["qbo_connected"]) is True
