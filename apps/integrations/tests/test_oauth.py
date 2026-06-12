from unittest.mock import MagicMock, patch

import pytest

from apps.integrations import services
from apps.integrations.factories import PodiumCredentialFactory
from apps.integrations.models import PodiumCredential

pytestmark = pytest.mark.django_db


# --- service: authorize URL ------------------------------------------------
def test_build_authorize_url_includes_params(settings):
    settings.PODIUM_CLIENT_ID = "cid"
    settings.PODIUM_REDIRECT_URI = "https://x.ngrok-free.dev/integrations/podium/callback/"
    settings.PODIUM_SCOPES = ["read_messages", "write_contacts"]
    url = services.build_authorize_url("st8")
    assert url.startswith("https://api.podium.com/oauth/authorize?")
    for fragment in ("client_id=cid", "response_type=code", "state=st8", "read_messages"):
        assert fragment in url


# --- service: token exchange + refresh -------------------------------------
def test_exchange_code_stores_credential():
    fake = MagicMock()
    fake.json.return_value = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_in": 36000,
        "scope": "read_messages",
    }
    with patch.object(services.requests, "post", return_value=fake) as post:
        cred = services.exchange_code("the-code")
    assert cred.access_token == "AT"
    assert cred.refresh_token == "RT"
    assert cred.is_expired is False
    assert PodiumCredential.current() == cred
    assert post.call_args.kwargs["data"]["grant_type"] == "authorization_code"
    assert post.call_args.kwargs["data"]["code"] == "the-code"


def test_refresh_keeps_refresh_token_and_updates_access():
    cred = PodiumCredentialFactory(access_token="old", refresh_token="RT")
    fake = MagicMock()
    fake.json.return_value = {"access_token": "NEW", "expires_in": 36000}
    with patch.object(services.requests, "post", return_value=fake) as post:
        updated = services.refresh(cred)
    assert updated.pk == cred.pk
    assert updated.access_token == "NEW"
    assert updated.refresh_token == "RT"
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"


# --- views -----------------------------------------------------------------
def test_authorize_redirects_to_podium(client, settings):
    settings.PODIUM_CLIENT_ID = "cid"
    resp = client.get("/integrations/podium/authorize/")
    assert resp.status_code == 302
    assert resp["Location"].startswith("https://api.podium.com/oauth/authorize")


def test_callback_rejects_bad_state(client):
    resp = client.get("/integrations/podium/callback/?code=x&state=wrong")
    assert resp.status_code == 400


def test_callback_happy_path_stores_token(client):
    session = client.session
    session["podium_oauth_state"] = "st8"
    session.save()
    fake = MagicMock()
    fake.json.return_value = {"access_token": "AT", "refresh_token": "RT", "expires_in": 36000}
    with patch.object(services.requests, "post", return_value=fake):
        resp = client.get("/integrations/podium/callback/?code=c&state=st8")
    assert resp.status_code == 200
    assert PodiumCredential.current().access_token == "AT"
