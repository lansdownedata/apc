"""LA client — token grants/caching + request/error plumbing (requests fully mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from apps.integrations import limoanywhere as la


@pytest.fixture(autouse=True)
def la_settings(settings):
    settings.LA_BASE_URL = "https://api.example.test"
    settings.LA_CLIENT_ID = "cid"
    settings.LA_CLIENT_SECRET = "csecret"
    settings.LA_COMPANY_ALIAS = "allpro"
    la._token_cache.clear()
    yield
    la._token_cache.clear()


def _response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.content = b"x" if json_data is not None else b""
    return resp


def test_not_configured_raises(settings):
    settings.LA_CLIENT_ID = ""
    with pytest.raises(la.LANotConfigured):
        la.get_token()


def test_client_credentials_token_fetch_and_cache():
    with patch.object(la, "requests") as req:
        req.post.return_value = _response(json_data={"access_token": "tok1", "expires_in": 3600})
        assert la.get_token() == "tok1"
        assert la.get_token() == "tok1"  # cached — only one HTTP call
    assert req.post.call_count == 1
    sent = req.post.call_args.kwargs["data"]
    assert sent["grant_type"] == "client_credentials"


def test_password_grant_sends_credentials():
    with patch.object(la, "requests") as req:
        req.post.return_value = _response(json_data={"access_token": "tok2", "expires_in": 3600})
        la.get_token(username="a@b.com", password="pw")
    sent = req.post.call_args.kwargs["data"]
    assert sent["grant_type"] == "password"
    assert sent["username"] == "a@b.com"
    assert sent["password"] == "pw"


def test_token_error_raises_la_api_error():
    with patch.object(la, "requests") as req:
        req.post.return_value = _response(status=401, text="bad client")
        with pytest.raises(la.LAAPIError) as exc:
            la.get_token()
    assert exc.value.status_code == 401


def test_register_customer_posts_signup():
    with patch.object(la, "requests") as req:
        req.post.return_value = _response(json_data={"access_token": "t", "expires_in": 3600})
        req.request.return_value = _response(json_data={"id": 12345, "number": "99119924"})
        result = la.register_customer({"first_name": "Jane"})
    assert result["id"] == 12345
    method, url = req.request.call_args.args[:2]
    assert method == "POST"
    assert url == "https://api.example.test/companies/allpro/customers/sign_up"


def test_api_error_carries_status_and_body():
    with patch.object(la, "requests") as req:
        req.post.return_value = _response(json_data={"access_token": "t", "expires_in": 3600})
        req.request.return_value = _response(status=422, text='{"error":"nope"}')
        with pytest.raises(la.LAAPIError) as exc:
            la.register_customer({})
    assert exc.value.status_code == 422
    assert "nope" in exc.value.body


def test_rate_lookup_and_booking_use_given_token():
    with patch.object(la, "requests") as req:
        req.request.return_value = _response(json_data={"results": []})
        la.rate_lookup({"passenger_count": 2}, token="custtok")
        la.create_booking({"search_result_id": 1}, token="custtok")
    for call in req.request.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == "Bearer custtok"


def test_subscribe_webhook_puts_uri():
    with patch.object(la, "requests") as req:
        req.request.return_value = _response()  # 204, empty body
        la.subscribe_webhook("https://x.test/webhooks/limoanywhere/abc/", token="custtok")
    method, url = req.request.call_args.args[:2]
    assert method == "PUT"
    assert url.endswith("/customers/self/subscriptions/webhook")
