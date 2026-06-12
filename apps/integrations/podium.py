"""Podium v4 API client — authenticated calls (auto-refreshing the token).

OAuth (token capture/refresh) lives in services.py; this module makes the
business calls: locations, messages, contacts. Higher-level orchestration
(creating Message rows, etc.) belongs in views/tasks, not here.
"""

import requests
from django.conf import settings

from . import services
from .models import PodiumCredential

API_BASE = "https://api.podium.com/v4"
TIMEOUT = 30


class PodiumNotConnected(Exception):
    """Raised when no Podium credential is stored (authorize first)."""


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
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def list_locations() -> dict:
    """All locations for the connected org (use to find the location UID)."""
    return _request("GET", "/locations")


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
