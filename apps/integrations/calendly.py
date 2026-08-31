"""Calendly-specific plumbing: timestamp parsing and the v2 API client.

Deliberately free of Django view/template concerns so both the public site and the
webhook processor can use it. Orchestration (creating Contacts/Leads) belongs in
webhooks.py, not here.
"""

from datetime import datetime

import requests
from django.conf import settings
from django.utils import timezone

API_BASE = "https://api.calendly.com"
TIMEOUT = 30

# The only two events this app knows how to process — see webhooks.process_calendly_webhook.
WEBHOOK_EVENTS = ["invitee.created", "invitee.canceled"]

# Path of the inbound endpoint, as routed in config/urls.py.
WEBHOOK_PATH = "/webhooks/calendly/"


class CalendlyNotConfigured(Exception):
    """No CALENDLY_API_TOKEN — generate one at Integrations → API & Webhooks."""


class CalendlyAPIError(Exception):
    """A non-2xx response from the Calendly API (carries status + body)."""


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
            f"{resp.status_code} {method} {resp.url} → {(resp.text or '')[:600]}"
        ) from exc
    return resp.json() if resp.content else {}


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
