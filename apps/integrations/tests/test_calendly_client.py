"""Calendly API v2 client + the webhook registration commands.

Everything here is mocked. The one live probe against the client's real token is
`manage.py calendly_webhooks`, which is read-only.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.core.management import call_command

from apps.integrations import calendly


def _response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}"
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_missing_token_raises_rather_than_calling_out(settings):
    settings.CALENDLY_API_TOKEN = ""
    with pytest.raises(calendly.CalendlyNotConfigured):
        calendly.current_user()


def test_current_user_unwraps_the_resource_envelope(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    body = {"resource": {"uri": "u1", "current_organization": "org1"}}
    with patch("apps.integrations.calendly.requests.request", return_value=_response(body)) as req:
        assert calendly.current_user()["current_organization"] == "org1"
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer pat"


def test_create_webhook_sends_the_documented_body(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_webhook(
            url="https://example.com/webhooks/calendly/",
            organization="org1",
            signing_key="cwk",
        )
    body = req.call_args.kwargs["json"]
    assert body["url"] == "https://example.com/webhooks/calendly/"
    assert body["organization"] == "org1"
    assert body["scope"] == "organization"
    assert body["signing_key"] == "cwk"
    assert body["events"] == ["invitee.created", "invitee.canceled"]
    # `user` is only legal on a user-scoped subscription.
    assert "user" not in body


def test_user_scope_includes_the_user_uri(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_webhook(
            url="https://example.com/webhooks/calendly/",
            organization="org1",
            signing_key="cwk",
            scope="user",
            user="https://api.calendly.com/users/u1",
        )
    assert req.call_args.kwargs["json"]["user"] == "https://api.calendly.com/users/u1"


def test_list_webhooks_passes_scope_and_org_as_query_params(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    with patch(
        "apps.integrations.calendly.requests.request", return_value=_response({"collection": []})
    ) as req:
        calendly.list_webhooks(organization="org1")
    assert req.call_args.kwargs["params"] == {"organization": "org1", "scope": "organization"}


def test_delete_webhook_targets_the_uuid(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.delete_webhook("abc-123")
    assert req.call_args.args[1].endswith("/webhook_subscriptions/abc-123")


def test_api_error_carries_status_and_body(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    resp = MagicMock()
    resp.status_code = 403
    resp.url = "https://api.calendly.com/users/me"
    resp.text = "forbidden"
    resp.raise_for_status.side_effect = requests.HTTPError("boom")
    with patch("apps.integrations.calendly.requests.request", return_value=resp):
        with pytest.raises(calendly.CalendlyAPIError) as exc:
            calendly.current_user()
    assert "403" in str(exc.value)
    assert "forbidden" in str(exc.value)


# --- management commands -------------------------------------------------------


def test_create_command_refuses_to_duplicate_a_subscription(settings):
    """Registering twice delivers every booking twice, and deleting the stray one
    needs its UUID — so refuse by default rather than making a mess to clean up."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    url = "https://example.com/webhooks/calendly/"
    err = StringIO()
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(
            calendly, "list_webhooks", return_value={"collection": [{"callback_url": url}]}
        ),
        patch.object(calendly, "create_webhook") as create,
    ):
        call_command("calendly_create_webhook", "--url", url, stderr=err)
    create.assert_not_called()
    assert "already points at" in err.getvalue()


def test_create_command_registers_when_there_is_no_clash(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(calendly, "list_webhooks", return_value={"collection": []}),
        patch.object(calendly, "create_webhook", return_value={"uri": "w1"}) as create,
    ):
        call_command(
            "calendly_create_webhook",
            "--url",
            "https://example.com/webhooks/calendly/",
            stdout=StringIO(),
        )
    assert create.call_args.kwargs["signing_key"] == "cwk"


def test_create_command_generates_a_key_and_tells_you_to_set_it(settings):
    """With no key configured the command invents one — useless unless it is printed,
    because the first delivery fails verification without it in the environment."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = ""
    out = StringIO()
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(calendly, "list_webhooks", return_value={"collection": []}),
        patch.object(calendly, "create_webhook", return_value={"uri": "w1"}) as create,
    ):
        call_command(
            "calendly_create_webhook",
            "--url",
            "https://example.com/webhooks/calendly/",
            stdout=out,
        )
    generated = create.call_args.kwargs["signing_key"]
    assert generated
    assert f"CALENDLY_WEBHOOK_SIGNING_KEY={generated}" in out.getvalue()


def test_list_command_reports_a_missing_token_without_a_traceback(settings):
    settings.CALENDLY_API_TOKEN = ""
    err = StringIO()
    call_command("calendly_webhooks", stderr=err)
    assert "CALENDLY_API_TOKEN" in err.getvalue()


def _subscription(**overrides):
    row = {
        "uri": "https://api.calendly.com/webhook_subscriptions/abc-123",
        "state": "active",
        "callback_url": "https://example.com/webhooks/calendly/",
        "events": ["invitee.created", "invitee.canceled"],
        "retry_started_at": None,
    }
    row.update(overrides)
    return {"collection": [row]}


def _run_list(rows, *args):
    out = StringIO()
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(calendly, "list_webhooks", return_value=rows),
    ):
        call_command("calendly_webhooks", *args, stdout=out)
    return out.getvalue()


def test_list_command_prints_each_subscription(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    printed = _run_list(_subscription())
    assert "abc-123" in printed
    assert "active" in printed
    assert "invitee.created" in printed


def test_list_command_flags_a_subscription_on_the_retry_clock(settings):
    """`retry_started_at` is Calendly's 24h disable clock — non-null means deliveries
    are failing and the subscription dies (needing recreation) if it isn't fixed.
    It is the one field on this listing that is genuinely urgent, so it must stand out.
    """
    settings.CALENDLY_API_TOKEN = "pat"
    printed = _run_list(_subscription(retry_started_at="2026-08-31T13:54:29.998267Z"))
    assert "RETRYING since 2026-08-31T13:54:29.998267Z" in printed


def test_list_command_stays_quiet_when_deliveries_are_healthy(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    assert "RETRYING" not in _run_list(_subscription())


def test_list_command_deletes_when_asked(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(calendly, "delete_webhook") as delete,
    ):
        call_command("calendly_webhooks", "--delete", "abc-123", stdout=StringIO())
    delete.assert_called_once_with("abc-123")


# --- callback URL resolution ---------------------------------------------------


def test_webhook_url_comes_from_public_base_url(settings):
    """One setting drives it, so moving leads.allprocharter.com -> www.allprocharter.com
    is a config change rather than a hunt for a hardcoded URL."""
    settings.PUBLIC_BASE_URL = "https://leads.allprocharter.com"
    assert calendly.webhook_url() == "https://leads.allprocharter.com/webhooks/calendly/"


def test_webhook_url_tolerates_a_trailing_slash(settings):
    """A doubled slash is a different URL to Calendly and would 301 on delivery."""
    settings.PUBLIC_BASE_URL = "https://leads.allprocharter.com/"
    assert calendly.webhook_url() == "https://leads.allprocharter.com/webhooks/calendly/"


def test_webhook_url_falls_back_to_ngrok_in_dev(settings):
    settings.PUBLIC_BASE_URL = ""
    settings.NGROK_HOST = "example.ngrok-free.dev"
    assert calendly.webhook_url() == "https://example.ngrok-free.dev/webhooks/calendly/"


def test_webhook_url_is_blank_when_nothing_is_configured(settings):
    settings.PUBLIC_BASE_URL = ""
    settings.NGROK_HOST = ""
    assert calendly.webhook_url() == ""


def test_create_command_defaults_to_the_configured_url(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_WEBHOOK_SIGNING_KEY = "cwk"
    settings.PUBLIC_BASE_URL = "https://leads.allprocharter.com"
    with (
        patch.object(
            calendly, "current_user", return_value={"current_organization": "org1", "uri": "u1"}
        ),
        patch.object(calendly, "list_webhooks", return_value={"collection": []}),
        patch.object(calendly, "create_webhook", return_value={"uri": "w1"}) as create,
    ):
        call_command("calendly_create_webhook", stdout=StringIO())
    assert create.call_args.kwargs["url"] == "https://leads.allprocharter.com/webhooks/calendly/"


def test_create_command_refuses_a_non_https_url(settings):
    """Calendly requires https, and prod's SECURE_SSL_REDIRECT would 301 an http
    delivery — which counts as a failure and starts the 24h disable clock."""
    settings.CALENDLY_API_TOKEN = "pat"
    err = StringIO()
    # current_user is patched so a bad URL cannot reach the network at all — the
    # command must reject it before it spends an API call.
    with (
        patch.object(calendly, "current_user") as me,
        patch.object(calendly, "create_webhook") as create,
    ):
        call_command(
            "calendly_create_webhook", "--url", "http://example.com/webhooks/calendly/", stderr=err
        )
    create.assert_not_called()
    me.assert_not_called()
    assert "must be https" in err.getvalue()
