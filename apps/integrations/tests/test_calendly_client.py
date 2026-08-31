"""Calendly API v2 client + the webhook registration commands.

Everything here is mocked. The one live probe against the client's real token is
`manage.py calendly_webhooks`, which is read-only.
"""

import json
from datetime import UTC, datetime
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


# --- scheduling: slots, questions, bookings -------------------------------------
#
# Every assertion below encodes something probed against the client's live account on
# 2026-08-31. Where a shape looks arbitrary, it is not — see the plan's Ground Truth.

EVENT_TYPE = "https://api.calendly.com/event_types/EAFTEGE2V6TLJSZT"


@pytest.fixture(autouse=True)
def _clear_calendly_cache():
    """`event_type_questions` caches, and LocMemCache outlives a single test."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _error_response(payload, status=400):
    resp = MagicMock()
    resp.status_code = status
    resp.url = "https://api.calendly.com/invitees"
    resp.text = json.dumps(payload)
    resp.content = resp.text.encode()
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = requests.HTTPError("boom")
    return resp


def test_available_times_sends_the_range_and_unwraps_the_collection(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    body = {"collection": [{"start_time": "2026-09-03T21:15:00Z", "status": "available"}]}
    with patch("apps.integrations.calendly.requests.request", return_value=_response(body)) as req:
        slots = calendly.available_times(
            start=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        )
    assert slots == body["collection"]
    params = req.call_args.kwargs["params"]
    assert params["event_type"] == EVENT_TYPE
    assert params["start_time"] == "2026-09-01T12:00:00.000000Z"
    assert params["end_time"] == "2026-09-05T12:00:00.000000Z"


def test_a_range_over_seven_days_raises_before_any_http_call(settings):
    """Calendly's own error for this is opaque, so we refuse locally and say why.

    The caller (the slots view) is expected to page; failing loudly here is what makes
    a missing page obvious instead of silently returning a short list.
    """
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request") as req:
        with pytest.raises(ValueError, match="7 days"):
            calendly.available_times(
                start=datetime(2026, 9, 1, tzinfo=UTC),
                end=datetime(2026, 9, 30, tzinfo=UTC),
            )
    req.assert_not_called()


def test_location_is_top_level_with_the_phone(settings):
    """Regression guard for the bug that cost hours: the 400 names
    event.location_configuration.kind, which is Calendly's INTERNAL model path, not the
    JSON key. Sending it there fails identically to sending nothing at all."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_invitee(
            start_time="2026-10-01T17:45:00.000000Z",
            name="Sarah",
            email="s@example.com",
            timezone="America/New_York",
            phone="+17035550101",
            answers=[],
        )
    body = req.call_args.kwargs["json"]
    assert body["location"] == {"kind": "outbound_call", "location": "+17035550101"}
    assert "location_configuration" not in json.dumps(body)


def test_the_booking_body_matches_the_probed_shape(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_invitee(
            start_time="2026-10-01T17:45:00.000000Z",
            name="Sarah",
            email="s@example.com",
            timezone="America/New_York",
            phone="+17035550101",
            answers=[],
        )
    assert req.call_args.args[0] == "POST"
    assert req.call_args.args[1].endswith("/invitees")
    body = req.call_args.kwargs["json"]
    assert body["event_type"] == EVENT_TYPE
    assert body["start_time"] == "2026-10-01T17:45:00.000000Z"
    assert body["invitee"] == {
        "email": "s@example.com",
        "name": "Sarah",
        "timezone": "America/New_York",
    }


def test_answers_are_a_sibling_of_invitee_and_always_carry_a_position(settings):
    """`questions_and_answers` is TOP level, not nested in `invitee`, and an entry
    without `position` gets the whole array rejected as "supplied parameters are
    invalid" — an error that names nothing useful."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    answers = [
        {"question": "Event Date", "answer": "2026-11-12", "position": 1},
        {"question": "What is your phone number?", "answer": "+1 703", "position": 2},
    ]
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_invitee(
            start_time="2026-10-01T17:45:00.000000Z",
            name="Sarah",
            email="s@example.com",
            timezone="America/New_York",
            phone="+17035550101",
            answers=answers,
        )
    body = req.call_args.kwargs["json"]
    assert body["questions_and_answers"] == answers
    assert "questions_and_answers" not in body["invitee"]
    assert all("position" in entry for entry in body["questions_and_answers"])


def test_an_answer_missing_its_position_is_refused_locally(settings):
    """Calendly's rejection is unattributable, so catch it where the cause is visible."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request") as req:
        with pytest.raises(ValueError, match="position"):
            calendly.create_invitee(
                start_time="2026-10-01T17:45:00.000000Z",
                name="Sarah",
                email="s@example.com",
                timezone="America/New_York",
                phone="+17035550101",
                answers=[{"question": "Event Date", "answer": "2026-11-12"}],
            )
    req.assert_not_called()


def test_a_taken_slot_raises_its_own_exception(settings):
    """`already_filled` on `event.start_time` is the authoritative race signal — the
    view turns this into a 409, and only this."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    resp = _error_response(
        {"details": [{"parameter": "event.start_time", "code": "already_filled"}]}
    )
    with patch("apps.integrations.calendly.requests.request", return_value=resp):
        with pytest.raises(calendly.CalendlySlotTaken):
            calendly.create_invitee(
                start_time="2026-10-01T17:45:00.000000Z",
                name="Sarah",
                email="s@example.com",
                timezone="America/New_York",
                phone="+17035550101",
                answers=[],
            )


def test_any_other_400_is_still_a_generic_api_error(settings):
    """A bad location must NOT masquerade as a taken slot — the UI would tell the
    visitor to pick another time and they would fail forever."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    resp = _error_response(
        {
            "details": [
                {
                    "parameter": "event.location_configuration.kind",
                    "code": "invalid_location_choice",
                }
            ]
        }
    )
    with patch("apps.integrations.calendly.requests.request", return_value=resp):
        with pytest.raises(calendly.CalendlyAPIError) as exc:
            calendly.create_invitee(
                start_time="2026-10-01T17:45:00.000000Z",
                name="Sarah",
                email="s@example.com",
                timezone="America/New_York",
                phone="+17035550101",
                answers=[],
            )
    assert not isinstance(exc.value, calendly.CalendlySlotTaken)


def test_slot_taken_is_a_subclass_so_broad_handlers_still_catch_it():
    assert issubclass(calendly.CalendlySlotTaken, calendly.CalendlyAPIError)


def test_api_error_exposes_the_parsed_body_not_just_a_string(settings):
    """`create_invitee` classifies on details[].code, so the parsed body has to survive
    the raise — re-parsing a formatted message would be its own bug."""
    settings.CALENDLY_API_TOKEN = "pat"
    resp = _error_response({"title": "Invalid Argument", "details": [{"code": "x"}]}, status=422)
    with patch("apps.integrations.calendly.requests.request", return_value=resp):
        with pytest.raises(calendly.CalendlyAPIError) as exc:
            calendly.current_user()
    assert exc.value.status == 422
    assert exc.value.body["title"] == "Invalid Argument"


def test_event_type_questions_reads_the_live_config(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    body = {
        "resource": {
            "custom_questions": [
                {"name": "Event Date", "type": "string", "position": 1, "required": True},
            ]
        }
    }
    with patch("apps.integrations.calendly.requests.request", return_value=_response(body)) as req:
        questions = calendly.event_type_questions()
    assert questions[0]["name"] == "Event Date"
    assert questions[0]["position"] == 1
    # The event type UUID, not the full URI, is what the path takes.
    assert req.call_args.args[1].endswith("/event_types/EAFTEGE2V6TLJSZT")


def test_event_type_questions_is_cached_across_calls(settings):
    """The question list changes when the client edits Calendly, not per request. The
    booking form renders it on every page view; uncached that is a rate-limit incident."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    body = {"resource": {"custom_questions": [{"name": "Event Date", "position": 1}]}}
    with patch("apps.integrations.calendly.requests.request", return_value=_response(body)) as req:
        first = calendly.event_type_questions()
        second = calendly.event_type_questions()
    assert first == second
    assert req.call_count == 1


def test_event_type_questions_is_empty_when_no_event_type_is_configured(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = ""
    with patch("apps.integrations.calendly.requests.request") as req:
        assert calendly.event_type_questions() == []
    req.assert_not_called()


# --- SMS reminders --------------------------------------------------------------
#
# `text_reminder_number` is documented as a field of `invitee` — NOT top level like
# `location` and `questions_and_answers`. Worth pinning: unknown request keys are
# silently IGNORED by this API rather than rejected (probed 2026-08-31 with a
# deliberately bogus key), so putting it in the wrong place would fail invisibly —
# no error, no SMS, nobody the wiser.


def test_a_reminder_number_rides_inside_invitee(settings):
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_invitee(
            start_time="2026-10-01T17:45:00.000000Z",
            name="Sarah",
            email="s@example.com",
            timezone="America/New_York",
            phone="+17035550101",
            answers=[],
            text_reminder_number="+17035550101",
        )
    body = req.call_args.kwargs["json"]
    assert body["invitee"]["text_reminder_number"] == "+17035550101"
    assert "text_reminder_number" not in body


def test_no_reminder_number_means_the_key_is_absent_entirely(settings):
    """Not an empty string — the customer did not consent, so there must be nothing
    for Calendly to interpret."""
    settings.CALENDLY_API_TOKEN = "pat"
    settings.CALENDLY_EVENT_TYPE_URI = EVENT_TYPE
    with patch("apps.integrations.calendly.requests.request", return_value=_response({})) as req:
        calendly.create_invitee(
            start_time="2026-10-01T17:45:00.000000Z",
            name="Sarah",
            email="s@example.com",
            timezone="America/New_York",
            phone="+17035550101",
            answers=[],
        )
    assert "text_reminder_number" not in json.dumps(req.call_args.kwargs["json"])
