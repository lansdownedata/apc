"""QuickBooks Online client (APC-41) — the OAuth connection and authenticated API calls.

Every endpoint and token rule here was checked against Intuit's own sources on 2026-10-24;
see docs/superpowers/specs/2026-09-19-billing-accounts/qbo-notes.md before changing one.

* Blank `QBO_CLIENT_ID` or `QBO_CLIENT_SECRET` = the feature is off (`enabled()` False), and
  `is_connected()` is only True for a live, unbroken connection — that is the flag the
  billing screens gate their sync chips on, not "a client id is set".
* Intuit rotates the refresh token. Every token response's refresh token is stored, under a
  row lock so two callers never spend the same one.
* `request()` refreshes before the access token expires and retries exactly once on a 401.
* Never log or render a token. Errors carry the HTTP status and Intuit's body.
"""

from __future__ import annotations

import base64
import logging
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import QboCredential

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"
TIMEOUT = 30
# Refresh this long before the access token runs out, so a call never lands on an expired one.
REFRESH_MARGIN = timedelta(minutes=5)


class QboAPIError(Exception):
    """A non-2xx answer from Intuit. `status` and `body` say what it was."""

    def __init__(self, status: int, body: str):
        super().__init__(f"QuickBooks returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class QboNotConnected(Exception):
    """No usable connection: never connected, disconnected, or broken (reconnect needed)."""


def enabled() -> bool:
    return bool(settings.QBO_CLIENT_ID and settings.QBO_CLIENT_SECRET)


def is_connected() -> bool:
    if not enabled():
        return False
    cred = QboCredential.objects.filter(singleton_id=1).first()
    return bool(cred and cred.is_live)


def _basic_auth() -> str:
    raw = f"{settings.QBO_CLIENT_ID}:{settings.QBO_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_call(data: dict) -> dict:
    resp = requests.request(
        "POST",
        TOKEN_URL,
        data=data,
        headers={"Authorization": _basic_auth(), "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise QboAPIError(resp.status_code, resp.text)
    return resp.json()


def _store(cred: QboCredential, tokens: dict) -> None:
    now = timezone.now()
    cred.set_tokens(
        access=tokens["access_token"],
        refresh=tokens["refresh_token"],
        access_expires_at=now + timedelta(seconds=int(tokens.get("expires_in") or 3600)),
        refresh_expires_at=(
            now + timedelta(seconds=int(tokens["x_refresh_token_expires_in"]))
            if tokens.get("x_refresh_token_expires_in")
            else None
        ),
    )


def authorize_url(state: str) -> str:
    params = {
        "client_id": settings.QBO_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": settings.QBO_REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, *, realm_id: str, user) -> QboCredential:
    """Trade the callback's code for tokens and record the connected company. Nothing is
    stored if Intuit refuses."""
    tokens = _token_call(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.QBO_REDIRECT_URI,
        }
    )
    with transaction.atomic():
        cred = QboCredential.objects.select_for_update().get_or_create(singleton_id=1)[0]
        _store(cred, tokens)
        cred.realm_id = realm_id
        cred.connected_at = timezone.now()
        cred.connected_by = user
        cred.broken_at = None
        cred.broken_reason = ""
        cred.save()
    return cred


def _mark_broken(cred: QboCredential, reason: str) -> None:
    from apps.notifications.models import Notification

    cred.broken_at = timezone.now()
    cred.broken_reason = reason[:255]
    cred.save(update_fields=["broken_at", "broken_reason", "updated_at"])
    Notification.notify(
        None,
        Notification.Kind.SYNC_FAILED,
        title="QuickBooks connection lost",
        detail=reason,
    )


def refresh() -> QboCredential:
    """Refresh under a row lock and store the rotated refresh token. A refused refresh
    (`invalid_grant`: expired or revoked) marks the connection broken and raises. The broken
    mark is written after the lock's transaction ends, so raising doesn't roll it back."""
    refused = None
    with transaction.atomic():
        cred = QboCredential.objects.select_for_update().get(singleton_id=1)
        if not cred.is_live:
            raise QboNotConnected("QuickBooks isn't connected.")
        try:
            tokens = _token_call(
                {"grant_type": "refresh_token", "refresh_token": cred.refresh_token}
            )
        except QboAPIError as exc:
            if not (exc.status in (400, 401) and "invalid_grant" in exc.body):
                raise
            refused = exc
        else:
            _store(cred, tokens)
            cred.save()
            return cred
    reason = "QuickBooks refused to renew the connection. Reconnect it in Settings."
    _mark_broken(QboCredential.objects.get(singleton_id=1), reason)
    raise QboNotConnected(reason) from refused


def ensure_fresh() -> QboCredential:
    """The live credential with an access token good for at least REFRESH_MARGIN. Re-reads
    under the lock before refreshing, so a caller that waited on another's refresh reuses it."""
    cred = QboCredential.objects.filter(singleton_id=1).first()
    if cred is None or not cred.is_live or not enabled():
        raise QboNotConnected("QuickBooks isn't connected.")
    if cred.access_expires_at and cred.access_expires_at - REFRESH_MARGIN > timezone.now():
        return cred
    with transaction.atomic():
        cred = QboCredential.objects.select_for_update().get(singleton_id=1)
        fresh = cred.access_expires_at and cred.access_expires_at - REFRESH_MARGIN > timezone.now()
    return cred if fresh else refresh()


def request(
    method: str, path: str, *, params: dict | None = None, json: dict | None = None
) -> dict:
    """An authenticated accounting API call: `{base}/v3/company/{realm}/{path}`. Refreshes a
    near-expired token first; on a 401, refreshes once and retries once."""
    cred = ensure_fresh()
    base = API_BASE.get(settings.QBO_ENVIRONMENT, API_BASE["sandbox"])
    url = f"{base}/v3/company/{cred.realm_id}/{path.lstrip('/')}"
    query = {"minorversion": MINOR_VERSION, **(params or {})}

    def call(token: str):
        return requests.request(
            method,
            url,
            params=query,
            json=json,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )

    resp = call(cred.access_token)
    if resp.status_code == 401:
        resp = call(refresh().access_token)
    if not resp.ok:
        raise QboAPIError(resp.status_code, resp.text)
    QboCredential.objects.filter(singleton_id=1).update(last_success_at=timezone.now())
    return resp.json()


def company_info() -> dict:
    cred = ensure_fresh()
    return request("GET", f"companyinfo/{cred.realm_id}").get("CompanyInfo", {})


def disconnect() -> None:
    """Revoke at Intuit (best effort) and clear the stored connection either way."""
    cred = QboCredential.objects.filter(singleton_id=1).first()
    if cred is None:
        return
    token = cred.refresh_token
    if token and enabled():
        try:
            resp = requests.request(
                "POST",
                REVOKE_URL,
                json={"token": token},
                headers={"Authorization": _basic_auth(), "Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if not resp.ok:
                logger.warning("QuickBooks revoke answered %s", resp.status_code)
        except requests.RequestException:
            logger.warning("QuickBooks revoke failed; clearing the local connection anyway")
    cred.set_tokens(access="", refresh="", access_expires_at=None, refresh_expires_at=None)
    cred.realm_id = ""
    cred.broken_at = None
    cred.broken_reason = ""
    cred.save()
