"""Podium v4 API client — authenticated calls (auto-refreshing the token).

OAuth (token capture/refresh) lives in services.py; this module makes the
business calls: locations, messages, contacts. Higher-level orchestration
(creating Message rows, etc.) belongs in views/tasks, not here.
"""

import logging
import re

import requests
from django.conf import settings
from django.core.cache import cache

from apps.core import outbound

from . import services
from .models import PodiumCredential

logger = logging.getLogger(__name__)

API_BASE = "https://api.podium.com/v4"
TIMEOUT = 30

# Podium's message webhooks identify the sender only by user UID, so outbound
# attribution needs a UID -> name lookup. The roster changes rarely; cache it.
USER_NAMES_CACHE_KEY = "podium:user-names"
USER_NAMES_CACHE_TTL = 60 * 60


class PodiumNotConnected(Exception):
    """Raised when no Podium credential is stored (authorize first)."""


class PodiumAPIError(Exception):
    """A non-2xx response from the Podium API (carries status + body)."""


# How `_request` words a refusal: "<status> <METHOD> <url> → <body>". The body is Podium's
# JSON, cut to 600 characters, so it may not parse.
_RAW_ERROR = re.compile(r"(\d{3}) (?:GET|POST|PUT|PATCH|DELETE) https?://\S+ → ")
_REASON = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')


def describe_error(text: str) -> str:
    """`text` with every raw Podium refusal in it replaced by a sentence staff can read:
    "Podium refused the message: Contact opted out" (CON-14, LEAD-15). Anything else comes
    back as it was. For display only; log the raw text, which is what a fix needs."""
    text = text or ""
    found = list(_RAW_ERROR.finditer(text))
    if not found:
        return text
    out = [text[: found[0].start()]]
    for i, match in enumerate(found):
        last = i + 1 == len(found)
        body = text[match.end() : len(text) if last else found[i + 1].start()]
        sep = ""
        if not last and body.endswith("; "):  # how several sends' errors are joined
            body, sep = body[:-2], "; "
        reason = _REASON.search(body)
        if reason:
            out.append(f"Podium refused the message: {reason.group(1)}{sep}")
        else:
            out.append(f"Podium refused the message ({match.group(1)}).{sep}")
    return "".join(out)


def invite_limited(exc: Exception) -> bool:
    """Whether `exc` is Podium's per-customer review-invite cap (429 "Customer has received
    too many invites per location."), as opposed to any other refusal or rate limit."""
    text = str(exc)
    return text.startswith("429 ") and "too many invites" in text.lower()


def get_credential() -> PodiumCredential:
    """The active credential, refreshed if it's at/near expiry."""
    cred = PodiumCredential.current()
    if cred is None:
        raise PodiumNotConnected(
            "No Podium credential — authorize at /integrations/podium/authorize/."
        )
    if cred.needs_refresh and cred.refresh_token:
        cred = services.refresh(cred)
    return cred


def _request(
    method: str, path: str, *, json=None, params=None, timeout: float | None = None
) -> dict:
    cred = get_credential()
    resp = requests.request(
        method,
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/json",
        },
        json=json,
        params=params,
        timeout=TIMEOUT if timeout is None else timeout,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise PodiumAPIError(
            f"{resp.status_code} {method} {resp.url} → {(resp.text or '')[:600]}"
        ) from exc
    return resp.json() if resp.content else {}


def list_locations() -> dict:
    """All locations for the connected org (use to find the location UID)."""
    return _request("GET", "/locations")


def list_users() -> dict:
    """All users on locations this token can see. Requires the `read_users` scope."""
    return _request("GET", "/users", params={"limit": 100})


def user_name_map(*, refresh: bool = False) -> dict[str, str]:
    """Podium user UID -> display name, cached for an hour.

    Returns {} rather than raising when Podium is unreachable or the token lacks
    `read_users`: sender attribution is cosmetic and must never break webhook
    ingestion or a send.
    """
    if not refresh:
        cached = cache.get(USER_NAMES_CACHE_KEY)
        if cached is not None:
            return cached

    try:
        users = list_users().get("data", [])
    except (PodiumAPIError, PodiumNotConnected) as exc:
        logger.warning("podium user_name_map: lookup failed (%s)", exc)
        return {}

    names: dict[str, str] = {}
    for user in users:
        uid = user.get("uid")
        if not uid:
            continue
        name = " ".join(p for p in (user.get("firstName"), user.get("lastName")) if p).strip()
        if name:
            names[uid] = name

    cache.set(USER_NAMES_CACHE_KEY, names, USER_NAMES_CACHE_TTL)
    return names


def send_message(
    *,
    identifier: str,
    body: str,
    channel_type: str = "phone",
    location_uid: str | None = None,
    internal: bool = False,
    timeout: float | None = None,
) -> dict:
    """Send an SMS/email through Podium (requires the write_messages scope).

    Refused with `OutboundDisabled` while the go-live switch is off (APC-90), unless
    `internal` says the recipient is one of our own staff.
    """
    if not outbound.allowed(kind=f"podium {channel_type}", recipient=identifier, internal=internal):
        raise outbound.OutboundDisabled
    return _request(
        "POST",
        "/messages",
        json={
            "channel": {"type": channel_type, "identifier": identifier},
            "body": body,
            "locationUid": location_uid or settings.PODIUM_LOCATION_UID,
        },
        timeout=timeout,
    )


def create_review_invitation(*, phone: str, location_uid: str | None = None) -> dict:
    """Create a single-use Podium review-invite link for one customer phone (requires the
    write_reviews scope). Podium only makes the link; the caller texts it. Returns the invite
    object (`uid`, `url`, `shortUrl`, ...), unwrapped from Podium's `data` envelope.

    Podium asks for a new link per send: one link shared between contacts breaks its
    reporting. Always a customer, so always behind the go-live switch (APC-90).
    """
    if not outbound.allowed(kind="podium review invitation", recipient=phone):
        raise outbound.OutboundDisabled
    out = _request(
        "POST",
        "/reviews/invites",
        json={"locationUid": location_uid or settings.PODIUM_LOCATION_UID, "phoneNumber": phone},
    )
    data = out.get("data")
    return data if isinstance(data, dict) else out


def create_webhook(
    *,
    url: str,
    event_types: list[str],
    secret: str = "",
    organization_uid: str | None = None,
    location_uid: str | None = None,
) -> dict:
    """Register a Podium webhook. One of organization_uid / location_uid is required
    (org wins if both are given). `secret` is what Podium signs events with."""
    body: dict = {"eventTypes": event_types, "url": url}
    if secret:
        body["secret"] = secret
    if organization_uid:
        body["organizationUid"] = organization_uid
    elif location_uid:
        body["locationUid"] = location_uid
    return _request("POST", "/webhooks", json=body)


def list_webhooks() -> dict:
    return _request("GET", "/webhooks")


PODIUM_WEBHOOK_PATH = "/webhooks/podium/"


def podium_webhooks() -> list[dict]:
    """The registered webhooks that deliver to this app's Podium receiver: the entries of
    `list_webhooks()["data"]` whose `url` ends in /webhooks/podium/. Each carries at least
    `uid`, `url` and `eventTypes`. Raises what `_request` raises."""
    entries = list_webhooks().get("data") or []
    return [e for e in entries if (e.get("url") or "").endswith(PODIUM_WEBHOOK_PATH)]


def delete_webhook(uid: str) -> dict:
    return _request("DELETE", f"/webhooks/{uid}")
