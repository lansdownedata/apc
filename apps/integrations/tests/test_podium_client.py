from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.integrations import podium
from apps.integrations.factories import PodiumCredentialFactory

pytestmark = pytest.mark.django_db


def _fresh_credential(access_token="AT"):
    return PodiumCredentialFactory(
        access_token=access_token, expires_at=timezone.now() + timedelta(hours=5)
    )


# --- get_credential (auto-refresh) -----------------------------------------
def test_get_credential_raises_when_not_connected():
    with pytest.raises(podium.PodiumNotConnected):
        podium.get_credential()


def test_get_credential_refreshes_when_expiring():
    cred = PodiumCredentialFactory(
        expires_at=timezone.now() + timedelta(seconds=10), refresh_token="RT"
    )
    with patch.object(podium.services, "refresh", return_value=cred) as refresh:
        podium.get_credential()
    refresh.assert_called_once()


def test_get_credential_returns_fresh_without_refresh():
    cred = _fresh_credential()
    with patch.object(podium.services, "refresh") as refresh:
        assert podium.get_credential() == cred
    refresh.assert_not_called()


# --- send_message ----------------------------------------------------------
def test_send_message_posts_to_messages(settings):
    settings.PODIUM_LOCATION_UID = "loc-1"
    _fresh_credential()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"uid": "msg-1"}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        out = podium.send_message(identifier="+15551234567", body="hi")
    assert out == {"uid": "msg-1"}
    method, url = req.call_args.args
    kwargs = req.call_args.kwargs
    assert method == "POST"
    assert url.endswith("/v4/messages")
    assert kwargs["headers"]["Authorization"] == "Bearer AT"
    assert kwargs["json"]["channel"] == {"type": "phone", "identifier": "+15551234567"}
    assert kwargs["json"]["body"] == "hi"
    assert kwargs["json"]["locationUid"] == "loc-1"


# --- list_locations --------------------------------------------------------
def test_list_locations_gets_locations():
    _fresh_credential()
    fake = MagicMock(content=b"{}")
    fake.json.return_value = {"data": [{"uid": "loc-1"}]}
    with patch.object(podium.requests, "request", return_value=fake) as req:
        out = podium.list_locations()
    method, url = req.call_args.args
    assert method == "GET"
    assert url.endswith("/v4/locations")
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer AT"
    assert out == {"data": [{"uid": "loc-1"}]}
