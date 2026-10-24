"""APC-41 — the QuickBooks Online connection. Every HTTP call is mocked; nothing here talks
to Intuit. Endpoints and token rules are from docs/superpowers/specs/2026-09-19-billing-
accounts/qbo-notes.md."""

import base64
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.integrations import qbo
from apps.integrations.models import QboCredential
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db

QBO = {
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_ENVIRONMENT": "sandbox",
    "QBO_REDIRECT_URI": "https://apc.example.com/integrations/quickbooks/callback/",
}
HTTP = "apps.integrations.qbo.requests.request"


def _resp(status=200, body=None):
    r = MagicMock(status_code=status, ok=200 <= status < 300, text=str(body or ""))
    r.json.return_value = body or {}
    return r


def _tokens(access="at-1", refresh="rt-1", expires_in=3600, refresh_in=8_726_400):
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires_in,
        "x_refresh_token_expires_in": refresh_in,
        "token_type": "bearer",
    }


def _connected(**over):
    over.setdefault("realm_id", "9130")
    cred = QboCredential.load()
    cred.realm_id = over["realm_id"]
    cred.set_tokens(
        access=over.get("access", "at-1"),
        refresh=over.get("refresh", "rt-1"),
        access_expires_at=over.get("access_expires_at", timezone.now() + timedelta(hours=1)),
        refresh_expires_at=timezone.now() + timedelta(days=100),
    )
    cred.connected_at = timezone.now()
    cred.save()
    return cred


# --- the gate -----------------------------------------------------------------------------


def test_the_feature_is_off_without_client_credentials():
    with override_settings(QBO_CLIENT_ID="", QBO_CLIENT_SECRET=""):
        assert qbo.enabled() is False
        assert qbo.is_connected() is False


@override_settings(**QBO)
def test_configured_is_not_the_same_as_connected():
    assert qbo.enabled() is True
    assert qbo.is_connected() is False
    _connected()
    assert qbo.is_connected() is True


@override_settings(**QBO)
def test_a_broken_connection_is_not_connected():
    cred = _connected()
    cred.broken_at = timezone.now()
    cred.save()

    assert qbo.is_connected() is False


# --- authorize + exchange ------------------------------------------------------------------


@override_settings(**QBO)
def test_the_authorize_url_carries_the_accounting_scope_and_state():
    url = qbo.authorize_url("state-123")

    parts = urlparse(url)
    q = parse_qs(parts.query)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == "https://appcenter.intuit.com/connect/oauth2"
    )
    assert q["client_id"] == ["client-id"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["com.intuit.quickbooks.accounting"]
    assert q["redirect_uri"] == [QBO["QBO_REDIRECT_URI"]]
    assert q["state"] == ["state-123"]


@override_settings(**QBO)
def test_exchanging_the_code_stores_encrypted_tokens_and_the_company():
    user = UserFactory()
    with patch(HTTP, return_value=_resp(200, _tokens())) as http:
        cred = qbo.exchange_code("the-code", realm_id="9130", user=user)

    method, url = http.call_args.args[:2]
    kwargs = http.call_args.kwargs
    assert (method, url) == ("POST", "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer")
    assert kwargs["data"] == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": QBO["QBO_REDIRECT_URI"],
    }
    expected = "Basic " + base64.b64encode(b"client-id:client-secret").decode()
    assert kwargs["headers"]["Authorization"] == expected
    assert (cred.realm_id, cred.connected_by, cred.broken_at) == ("9130", user, None)
    assert cred.access_token == "at-1" and cred.refresh_token == "rt-1"
    raw = QboCredential.objects.values_list("access_token_enc", "refresh_token_enc").get()
    assert "at-1" not in raw[0] and "rt-1" not in raw[1]  # encrypted at rest


@override_settings(**QBO)
def test_a_failed_exchange_raises_and_stores_nothing():
    with patch(HTTP, return_value=_resp(400, {"error": "invalid_grant"})):
        with pytest.raises(qbo.QboAPIError):
            qbo.exchange_code("bad", realm_id="9130", user=UserFactory())

    assert qbo.is_connected() is False


# --- refresh --------------------------------------------------------------------------------


@override_settings(**QBO)
def test_refresh_stores_the_new_rotated_refresh_token():
    _connected(refresh="rt-old")

    with patch(HTTP, return_value=_resp(200, _tokens(access="at-2", refresh="rt-new"))) as http:
        cred = qbo.refresh(force=True)

    assert http.call_args.kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-old",
    }
    assert (cred.access_token, cred.refresh_token) == ("at-2", "rt-new")
    assert QboCredential.load().refresh_token == "rt-new"


@override_settings(**QBO)
def test_an_invalid_grant_marks_the_connection_broken_and_notifies():
    _connected()

    with patch(HTTP, return_value=_resp(400, {"error": "invalid_grant"})):
        with pytest.raises(qbo.QboNotConnected):
            qbo.refresh(force=True)

    cred = QboCredential.load()
    assert cred.broken_at is not None and "reconnect" in cred.broken_reason.lower()
    assert Notification.objects.filter(title__icontains="QuickBooks").exists()


@override_settings(**QBO)
def test_a_refresh_waits_for_the_one_already_running_and_reuses_its_token():
    """Two jobs racing must not both spend the same refresh token: the second re-reads under
    the lock and, finding a fresh access token, doesn't refresh again."""
    _connected(access_expires_at=timezone.now() - timedelta(minutes=1))
    with patch(HTTP, return_value=_resp(200, _tokens(access="at-2", refresh="rt-2"))) as http:
        qbo.refresh()
        qbo.ensure_fresh()  # the second caller: token is now fresh

    assert http.call_count == 1


# --- API requests ---------------------------------------------------------------------------


@override_settings(**QBO)
def test_a_request_goes_to_the_sandbox_company_with_minor_version_75():
    _connected()

    with patch(
        HTTP, return_value=_resp(200, {"CompanyInfo": {"CompanyName": "Sandbox Co"}})
    ) as http:
        body = qbo.request("GET", "companyinfo/9130")

    method, url = http.call_args.args[:2]
    assert (method, url) == (
        "GET",
        "https://sandbox-quickbooks.api.intuit.com/v3/company/9130/companyinfo/9130",
    )
    assert http.call_args.kwargs["params"]["minorversion"] == "75"
    assert http.call_args.kwargs["headers"]["Authorization"] == "Bearer at-1"
    assert body["CompanyInfo"]["CompanyName"] == "Sandbox Co"
    assert QboCredential.load().last_success_at is not None


@override_settings(**{**QBO, "QBO_ENVIRONMENT": "production"})
def test_production_uses_the_production_host():
    _connected()

    with patch(HTTP, return_value=_resp(200, {})) as http:
        qbo.request("GET", "companyinfo/9130")

    assert http.call_args.args[1].startswith("https://quickbooks.api.intuit.com/v3/company/9130/")


@override_settings(**QBO)
def test_an_expired_access_token_is_refreshed_before_the_call():
    _connected(access_expires_at=timezone.now() - timedelta(minutes=1))
    calls = [_resp(200, _tokens(access="at-2", refresh="rt-2")), _resp(200, {"ok": True})]

    with patch(HTTP, side_effect=calls) as http:
        qbo.request("GET", "companyinfo/9130")

    assert http.call_args.kwargs["headers"]["Authorization"] == "Bearer at-2"


@override_settings(**QBO)
def test_a_401_refreshes_once_and_retries_once():
    _connected()
    calls = [
        _resp(401, {"fault": "auth"}),
        _resp(200, _tokens(access="at-2", refresh="rt-2")),
        _resp(200, {"ok": True}),
    ]

    with patch(HTTP, side_effect=calls) as http:
        assert qbo.request("GET", "companyinfo/9130") == {"ok": True}

    assert http.call_count == 3


@override_settings(**QBO)
def test_a_second_401_is_an_error_not_a_loop():
    _connected()
    calls = [_resp(401, {}), _resp(200, _tokens(access="at-2", refresh="rt-2")), _resp(401, {})]

    with patch(HTTP, side_effect=calls) as http, pytest.raises(qbo.QboAPIError) as err:
        qbo.request("GET", "companyinfo/9130")

    assert http.call_count == 3
    assert err.value.status == 401


@override_settings(**QBO)
def test_a_request_without_a_connection_raises_not_connected():
    with patch(HTTP) as http, pytest.raises(qbo.QboNotConnected):
        qbo.request("GET", "companyinfo/1")

    http.assert_not_called()


# --- disconnect -----------------------------------------------------------------------------


@override_settings(**QBO)
def test_disconnect_revokes_the_refresh_token_and_clears_it():
    _connected(refresh="rt-1")

    with patch(HTTP, return_value=_resp(200, {})) as http:
        qbo.disconnect()

    method, url = http.call_args.args[:2]
    assert (method, url) == ("POST", "https://developer.api.intuit.com/v2/oauth2/tokens/revoke")
    assert http.call_args.kwargs["json"] == {"token": "rt-1"}
    assert qbo.is_connected() is False
    assert QboCredential.load().refresh_token == ""


@override_settings(**QBO)
def test_disconnect_still_clears_local_tokens_when_intuit_fails():
    _connected()

    with patch(HTTP, side_effect=qbo.requests.ConnectionError("down")):
        qbo.disconnect()

    assert qbo.is_connected() is False


# --- the smoke test -------------------------------------------------------------------------

from io import StringIO  # noqa: E402

from django.core.management import call_command  # noqa: E402


@override_settings(**QBO)
def test_the_smoke_test_prints_the_connected_company():
    _connected()
    out = StringIO()

    with patch(HTTP, return_value=_resp(200, {"CompanyInfo": {"CompanyName": "Sandbox Co"}})):
        call_command("qbo_smoke_test", stdout=out)

    text = out.getvalue()
    assert "Sandbox Co" in text and "9130" in text and "sandbox" in text
    assert "at-1" not in text and "rt-1" not in text  # never a token


@override_settings(**QBO)
def test_the_smoke_test_says_what_to_do_when_not_connected():
    with pytest.raises(SystemExit):
        call_command("qbo_smoke_test", stdout=StringIO(), stderr=StringIO())


# --- review fixes (2026-10-24) ----------------------------------------------------------------

from django.db import transaction  # noqa: E402


@override_settings(**QBO)
def test_a_refresh_inside_an_outer_transaction_is_refused():
    """The rotated refresh token must commit on its own: inside a caller's transaction a
    rollback would lose it after Intuit had already rotated, and kill the connection."""
    _connected()

    with patch(HTTP) as http, pytest.raises(RuntimeError), transaction.atomic():
        qbo.refresh(force=True)

    http.assert_not_called()


@override_settings(**QBO)
def test_a_refresh_never_spends_the_token_again_when_another_caller_just_refreshed():
    _connected()  # the access token is fresh

    with patch(HTTP) as http:
        qbo.refresh()  # force=False: re-checks under the lock

    http.assert_not_called()


@override_settings(**QBO)
def test_a_refused_refresh_notifies_once_and_never_spends_the_dead_token_again():
    _connected()

    with patch(HTTP, return_value=_resp(400, {"error": "invalid_grant"})) as http:
        for _ in range(2):
            with pytest.raises(qbo.QboNotConnected):
                qbo.refresh(force=True)

    assert http.call_count == 1
    assert Notification.objects.filter(title__icontains="QuickBooks").count() == 1


@override_settings(**QBO)
def test_rejected_app_credentials_mark_the_connection_broken_and_notify():
    _connected()

    with patch(HTTP, return_value=_resp(401, {"error": "invalid_client"})):
        with pytest.raises(qbo.QboNotConnected):
            qbo.refresh(force=True)

    cred = QboCredential.load()
    assert cred.broken_at is not None and "credentials" in cred.broken_reason
    assert Notification.objects.filter(title__icontains="QuickBooks").exists()


@override_settings(**QBO)
def test_an_intuit_outage_on_refresh_is_an_error_but_not_a_broken_connection():
    _connected()

    with patch(HTTP, return_value=_resp(503, {"fault": "down"})):
        with pytest.raises(qbo.QboAPIError):
            qbo.refresh(force=True)

    assert qbo.is_connected() is True


@override_settings(**QBO)
def test_network_errors_come_back_as_qbo_errors_not_raw_requests_ones():
    _connected()

    with patch(HTTP, side_effect=qbo.requests.ConnectionError("down")):
        with pytest.raises(qbo.QboAPIError) as err:
            qbo.request("GET", "companyinfo/9130")

    assert err.value.status == 0


@override_settings(**QBO)
def test_a_rate_limit_waits_once_and_retries():
    _connected()
    limited = _resp(429, {"fault": "throttled"})
    limited.headers = {"Retry-After": "1"}

    with (
        patch(HTTP, side_effect=[limited, _resp(200, {"ok": True})]) as http,
        patch.object(qbo.time, "sleep") as sleep,
    ):
        assert qbo.request("GET", "companyinfo/9130") == {"ok": True}

    assert http.call_count == 2
    sleep.assert_called_once_with(1)


@override_settings(**QBO)
def test_a_second_rate_limit_raises_rather_than_looping():
    _connected()
    limited = _resp(429, {})
    limited.headers = {"Retry-After": "120"}

    with (
        patch(HTTP, side_effect=[limited, limited]) as http,
        patch.object(qbo.time, "sleep") as sleep,
    ):
        with pytest.raises(qbo.QboRateLimited) as err:
            qbo.request("GET", "companyinfo/9130")

    assert http.call_count == 2
    assert (
        sleep.call_args.args[0] <= qbo.MAX_RATE_LIMIT_WAIT
    )  # never sleeps two minutes in a request
    assert err.value.retry_after == 120


@override_settings(**QBO)
def test_unreadable_tokens_after_a_secret_key_change_mark_the_connection_broken():
    _connected()

    with override_settings(SECRET_KEY="a-different-secret-key-entirely"):
        with patch(HTTP) as http, pytest.raises(qbo.QboNotConnected):
            qbo.request("GET", "companyinfo/9130")
        http.assert_not_called()
        cred = QboCredential.load()
        assert cred.broken_at is not None and "secret key" in cred.broken_reason.lower()


@override_settings(**QBO)
def test_disconnect_still_works_when_the_tokens_cannot_be_read():
    _connected()

    with override_settings(SECRET_KEY="a-different-secret-key-entirely"), patch(HTTP) as http:
        qbo.disconnect()

    http.assert_not_called()  # nothing readable to revoke
    assert qbo.is_connected() is False


@override_settings(**QBO)
def test_the_401_retry_uses_and_keeps_the_new_tokens():
    _connected()
    calls = [_resp(401, {}), _resp(200, _tokens(access="at-2", refresh="rt-2")), _resp(200, {})]

    with patch(HTTP, side_effect=calls) as http:
        qbo.request("GET", "companyinfo/9130")

    assert http.call_args.kwargs["headers"]["Authorization"] == "Bearer at-2"
    assert QboCredential.load().refresh_token == "rt-2"


@override_settings(**QBO)
def test_reconnecting_resets_the_last_successful_call():
    cred = _connected()
    cred.last_success_at = timezone.now()
    cred.save()

    with patch(HTTP, return_value=_resp(200, _tokens())):
        qbo.exchange_code("c", realm_id="4620", user=UserFactory())

    assert QboCredential.load().last_success_at is None


@override_settings(**QBO)
def test_refresh_with_no_connection_at_all_is_not_connected():
    with pytest.raises(qbo.QboNotConnected):
        qbo.refresh()
