"""Calendly-specific plumbing: timestamp parsing and the v2 API client.

Deliberately free of Django view/template concerns so both the public site and the
webhook processor can use it. Orchestration (creating Contacts/Leads) belongs in
webhooks.py, not here.
"""

from datetime import UTC, datetime, timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

API_BASE = "https://api.calendly.com"
TIMEOUT = 30

# The only two events this app knows how to process — see webhooks.process_calendly_webhook.
WEBHOOK_EVENTS = ["invitee.created", "invitee.canceled"]

# Path of the inbound endpoint, as routed in config/urls.py.
WEBHOOK_PATH = "/webhooks/calendly/"

# `GET /event_type_available_times` refuses a wider window than this, with an error
# that does not say so. A month view is therefore several calls, not one.
MAX_SLOT_RANGE = timedelta(days=7)

# The custom-question list changes when the client edits it in Calendly — rarely, and
# never per request. The booking form needs it on every page view.
QUESTIONS_CACHE_SECONDS = 300


class CalendlyNotConfigured(Exception):
    """No CALENDLY_API_TOKEN — generate one at Integrations → API & Webhooks."""


class CalendlyAPIError(Exception):
    """A non-2xx response from the Calendly API (carries status + body).

    `body` is the PARSED payload, not just the formatted message. Callers classify on
    `details[].code`, and re-parsing a human-readable string to recover a machine-readable
    code would be its own bug. It is `{}` when the response was not JSON.
    """

    def __init__(self, message: str, *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}

    def has_code(self, code: str) -> bool:
        return any(d.get("code") == code for d in (self.body.get("details") or []))


class CalendlySlotTaken(CalendlyAPIError):
    """The slot went while the visitor was filling the form.

    Calendly is the ONLY authority on this — a local hold cannot see a booking made on
    calendly.com or a meeting that just appeared in the host's own calendar. Kept
    distinct from its parent so the view can answer 409 for exactly this and 502 for
    everything else; a misclassified location error would otherwise tell the visitor to
    pick another time, forever.
    """


def parse_start_time(raw: str) -> datetime | None:
    """A Calendly ISO 8601 timestamp as an aware datetime, or None.

    Calendly sends UTC with a trailing Z and microseconds
    ("2026-09-08T18:30:00.000000Z"), which `datetime.fromisoformat` handles natively
    on 3.11+. A naive value is rejected rather than assumed to be UTC: guessing the
    zone produces a confirmation that is plausibly, silently wrong by hours.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return None if timezone.is_naive(parsed) else parsed


def _request(method: str, path: str, *, json=None, params=None) -> dict:
    token = settings.CALENDLY_API_TOKEN
    if not token:
        raise CalendlyNotConfigured(
            "CALENDLY_API_TOKEN is not set — generate a personal access token at "
            "Calendly → Integrations → API & Webhooks."
        )
    resp = requests.request(
        method,
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=json,
        params=params,
        timeout=TIMEOUT,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise CalendlyAPIError(
            f"{resp.status_code} {method} {resp.url} → {(resp.text or '')[:600]}",
            status=resp.status_code,
            body=_parse_body(resp),
        ) from exc
    return resp.json() if resp.content else {}


def _parse_body(resp) -> dict:
    """The error payload as a dict, or {} — an error page is never worth a traceback."""
    try:
        parsed = resp.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def current_user() -> dict:
    """The token owner.

    `current_organization` on the result is the org URI every webhook call needs —
    there is no other way to discover it.
    """
    return _request("GET", "/users/me").get("resource", {})


def list_webhooks(*, organization: str, scope: str = "organization", user: str = "") -> dict:
    params = {"organization": organization, "scope": scope}
    if scope == "user":
        params["user"] = user
    return _request("GET", "/webhook_subscriptions", params=params)


def create_webhook(
    *,
    url: str,
    organization: str,
    signing_key: str,
    scope: str = "organization",
    user: str = "",
    events: list[str] | None = None,
) -> dict:
    """Register a webhook subscription. There is no UI for this in Calendly.

    `signing_key` is OURS to choose — it is the HMAC key Calendly will sign
    deliveries with, and it must already be in the app's config or the first
    delivery fails verification.
    """
    body: dict = {
        "url": url,
        "events": events or list(WEBHOOK_EVENTS),
        "organization": organization,
        "scope": scope,
        "signing_key": signing_key,
    }
    if scope == "user":
        body["user"] = user
    return _request("POST", "/webhook_subscriptions", json=body)


def delete_webhook(uuid: str) -> dict:
    return _request("DELETE", f"/webhook_subscriptions/{uuid}")


def webhook_url() -> str:
    """The callback URL to register with Calendly, or "" if nothing is configured.

    Derived from PUBLIC_BASE_URL — the same setting quote links, invite emails and
    the deposit report already use — so moving the site (leads.allprocharter.com →
    www.allprocharter.com) is one config change rather than a hunt for a hardcoded
    URL. Falls back to NGROK_HOST for dev, where PUBLIC_BASE_URL is usually unset.

    The trailing slash is not cosmetic: Django's APPEND_SLASH would 301 a delivery
    sent to the slashless path, and Calendly counts a 3xx as a failed delivery.
    """
    base = (getattr(settings, "PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base:
        host = (getattr(settings, "NGROK_HOST", "") or "").strip()
        base = f"https://{host}" if host else ""
    return f"{base}{WEBHOOK_PATH}" if base else ""


# --- scheduling ----------------------------------------------------------------
#
# Booking on the visitor's behalf. Calendly still owns the calendar, the invite email,
# reminders, reschedule and cancel — this only creates the invitee. A booking made here
# fires `invitee.created` exactly as a UI booking does (probed 2026-08-31), so the
# Contact/Lead is webhooks.py's job and must NOT be duplicated by the caller.


def _iso(moment: datetime) -> str:
    """Calendly's timestamp format: UTC, six-digit microseconds, trailing Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def event_type_uuid() -> str:
    """The trailing UUID of CALENDLY_EVENT_TYPE_URI — path calls take it, not the URI."""
    uri = (getattr(settings, "CALENDLY_EVENT_TYPE_URI", "") or "").strip()
    return uri.rstrip("/").rsplit("/", 1)[-1] if uri else ""


def available_times(*, start: datetime, end: datetime) -> list[dict]:
    """Bookable slots for the configured event type, as Calendly computes them.

    Deliberately not computed from availability schedules + busy times: doing that means
    owning buffers, minimum notice, date overrides and DST, each a way to offer a slot
    Calendly then rejects. This endpoint returns the finished answer.

    Raises ValueError past MAX_SLOT_RANGE rather than letting the API answer, because
    its error for an over-wide window does not mention the window. Callers page.
    """
    if end - start > MAX_SLOT_RANGE:
        raise ValueError(
            f"Calendly caps event_type_available_times at 7 days per call; "
            f"{(end - start).days} days were requested. Page the range instead."
        )
    data = _request(
        "GET",
        "/event_type_available_times",
        params={
            "event_type": settings.CALENDLY_EVENT_TYPE_URI,
            "start_time": _iso(start),
            "end_time": _iso(end),
        },
    )
    return data.get("collection", [])


def event_type_questions() -> list[dict]:
    """The event type's live `custom_questions`, cached.

    Read rather than hardcoded on purpose: `position` is what the booking API matches
    answers on, and it is the client's to change in Calendly at any time. A hardcoded
    position keeps working right up until he reorders a question, then fails silently.
    """
    uuid = event_type_uuid()
    if not uuid:
        return []
    key = f"calendly:event-type-questions:{uuid}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    resource = _request("GET", f"/event_types/{uuid}").get("resource", {})
    questions = resource.get("custom_questions") or []
    cache.set(key, questions, QUESTIONS_CACHE_SECONDS)
    return questions


def create_invitee(
    *,
    start_time: str,
    name: str,
    email: str,
    timezone: str,
    phone: str,
    answers: list[dict],
) -> dict:
    """Book the slot. Returns Calendly's invitee resource.

    Two shapes here are load-bearing and both were paid for in debugging time:

    `location` is TOP LEVEL. Calendly's 400 names `event.location_configuration.kind`,
    which is its internal model path, not the JSON key — and sending it under that key
    fails identically to omitting the location entirely, so the error reads like
    "outbound_call is unsupported" when it is nothing of the kind.

    `questions_and_answers` is likewise top level, a SIBLING of `invitee`, and every
    entry needs `position`. Without it the whole array is rejected as "supplied
    parameters are invalid", naming no field.

    Location validation runs before slot availability, so a malformed location masks
    every other error in the body.
    """
    missing = [entry for entry in answers if entry.get("position") is None]
    if missing:
        raise ValueError(
            "every questions_and_answers entry needs a `position` from the live event "
            f"type; {len(missing)} entry/entries had none: "
            f"{[e.get('question') for e in missing]}"
        )
    body: dict = {
        "event_type": settings.CALENDLY_EVENT_TYPE_URI,
        "start_time": start_time,
        "location": {"kind": "outbound_call", "location": phone},
        "invitee": {"email": email, "name": name, "timezone": timezone},
    }
    if answers:
        body["questions_and_answers"] = list(answers)
    try:
        return _request("POST", "/invitees", json=body).get("resource", {})
    except CalendlySlotTaken:
        raise
    except CalendlyAPIError as exc:
        if exc.has_code("already_filled"):
            raise CalendlySlotTaken(str(exc), status=exc.status, body=exc.body) from exc
        raise
