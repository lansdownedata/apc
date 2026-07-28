"""Podium v4 API client — authenticated calls (auto-refreshing the token).

OAuth (token capture/refresh) lives in services.py; this module makes the
business calls: locations, messages, contacts. Higher-level orchestration
(creating Message rows, etc.) belongs in views/tasks, not here.
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

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


def _request(method: str, path: str, *, json=None, params=None) -> dict:
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
        timeout=TIMEOUT,
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
    *, identifier: str, body: str, channel_type: str = "phone", location_uid: str | None = None
) -> dict:
    """Send an SMS/email through Podium (requires the write_messages scope)."""
    return _request(
        "POST",
        "/messages",
        json={
            "channel": {"type": channel_type, "identifier": identifier},
            "body": body,
            "locationUid": location_uid or settings.PODIUM_LOCATION_UID,
        },
    )


def create_review_invitation(
    *, phone: str, first_name: str, last_name: str, location_uid: str | None = None
) -> dict:
    """Send a Podium review-invitation SMS to a customer.

    VERIFY-LIVE: the `/review_invitations` path and request/response shape below are a
    documented best-guess (Podium's `review_invitecreate` reference page isn't
    machine-readable) — probe this against the connected test account before relying on
    it in prod and correct path/body/response keys if the guess is off.
    """
    return _request(
        "POST",
        "/review_invitations",
        json={
            "locationUid": location_uid or settings.PODIUM_LOCATION_UID,
            "contact": {
                "phoneNumber": phone,
                "firstName": first_name,
                "lastName": last_name,
            },
        },
    )


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


def delete_webhook(uid: str) -> dict:
    return _request("DELETE", f"/webhooks/{uid}")
