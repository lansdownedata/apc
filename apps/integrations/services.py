"""Podium OAuth 2.0 (authorization-code) — token exchange + refresh.

Tokens are stored in PodiumCredential. Access tokens last ~10h; callers should
check `credential.needs_refresh` and call `refresh()` before live API calls.
"""

from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import PodiumCredential

AUTHORIZE_URL = "https://api.podium.com/oauth/authorize"
TOKEN_URL = "https://api.podium.com/oauth/token"
DEFAULT_EXPIRES_IN = 36000  # 10 hours, per Podium docs
TIMEOUT = 30


def build_authorize_url(state: str) -> str:
    """The URL to send the user to so they can grant access."""
    params = {
        "client_id": settings.PODIUM_CLIENT_ID,
        "redirect_uri": settings.PODIUM_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(settings.PODIUM_SCOPES),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> PodiumCredential:
    """Exchange an authorization code for tokens and store them."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.PODIUM_CLIENT_ID,
            "client_secret": settings.PODIUM_CLIENT_SECRET,
            "redirect_uri": settings.PODIUM_REDIRECT_URI,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return _store_tokens(resp.json())


def refresh(credential: PodiumCredential) -> PodiumCredential:
    """Use the stored refresh token to obtain a fresh access token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token,
            "client_id": settings.PODIUM_CLIENT_ID,
            "client_secret": settings.PODIUM_CLIENT_SECRET,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return _store_tokens(resp.json(), credential=credential)


def _store_tokens(data: dict, credential: PodiumCredential | None = None) -> PodiumCredential:
    credential = credential or PodiumCredential()
    credential.access_token = data["access_token"]
    # Refresh-token responses may omit a new refresh token — keep the existing one.
    credential.refresh_token = data.get("refresh_token") or credential.refresh_token
    credential.scopes = data.get("scope") or credential.scopes or " ".join(settings.PODIUM_SCOPES)
    expires_in = int(data.get("expires_in", DEFAULT_EXPIRES_IN))
    credential.expires_at = timezone.now() + timedelta(seconds=expires_in)
    credential.organization_uid = credential.organization_uid or settings.PODIUM_ORGANIZATION_UID
    credential.location_uid = credential.location_uid or settings.PODIUM_LOCATION_UID
    credential.save()
    return credential
