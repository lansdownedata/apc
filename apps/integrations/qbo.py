"""QuickBooks Online client (APC-41) — the OAuth connection and authenticated API calls.

Every endpoint and token rule here was checked against Intuit's own sources on 2026-10-24;
see docs/superpowers/specs/2026-09-19-billing-accounts/qbo-notes.md before changing one.

* Blank `QBO_CLIENT_ID` or `QBO_CLIENT_SECRET` = the feature is off (`enabled()` False), and
  `is_connected()` is only True for a live, unbroken connection — that is the flag the
  billing screens gate their sync chips on, not "a client id is set".
* Intuit rotates the refresh token. Every token response's refresh token is stored in its
  own durable transaction, under the row lock that also re-checks freshness, so two callers
  never spend the same token and a caller's rollback can never lose a rotated one. Calling a
  refresh inside an outer transaction is refused (RuntimeError) on purpose.
* A refused refresh — expired/revoked grant, rejected app credentials, or tokens the server
  can no longer decrypt (SECRET_KEY changed) — marks the connection broken and notifies,
  once. Only a reconnect fixes it. An Intuit outage is an error, not a broken connection.
* `request()` refreshes before expiry, retries exactly once on a 401, and waits once (capped)
  on a 429 before raising `QboRateLimited`. Network failures come back as `QboAPIError(0, …)`.
* Never log or render a token. Errors carry the HTTP status and Intuit's body.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from datetime import timedelta
from urllib.parse import urlencode

import requests
from cryptography.fernet import InvalidToken
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
TIMEOUT = 15
# Refresh this long before the access token runs out, so a call never lands on an expired one.
REFRESH_MARGIN = timedelta(minutes=5)
# Never block a web request or a cron run for long on a 429; longer waits raise QboRateLimited.
MAX_RATE_LIMIT_WAIT = 5
REALM_ID = re.compile(r"^\d{1,32}$")

_BROKEN_GRANT = "QuickBooks refused to renew the connection. Reconnect it in Settings."
_BROKEN_CLIENT = (
    "QuickBooks rejected the app's credentials (QBO_CLIENT_ID / QBO_CLIENT_SECRET). Check them,"
    " then reconnect in Settings."
)
_BROKEN_KEY = (
    "The stored QuickBooks tokens can't be read — the server's secret key changed. Reconnect it"
    " in Settings."
)


class QboAPIError(Exception):
    """A non-2xx answer from Intuit, or no answer at all (`status` 0)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"QuickBooks returned {status}: {body[:300]}")
        self.status = status
        self.body = body


class QboRateLimited(QboAPIError):
    """Intuit is throttling us. `retry_after` is how long it asked us to wait (seconds)."""

    def __init__(self, body: str, retry_after: int):
        super().__init__(429, body)
        self.retry_after = retry_after


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


def _http(method: str, url: str, **kwargs):
    """One HTTP call; a network failure becomes QboAPIError(0) so callers catch one family."""
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise QboAPIError(0, f"network: {exc.__class__.__name__}") from exc


def _token_call(data: dict) -> dict:
    resp = _http(
        "POST",
        TOKEN_URL,
        data=data,
        headers={"Authorization": _basic_auth(), "Accept": "application/json"},
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


_TOKEN_FIELDS = ["access_token_enc", "refresh_token_enc", "access_expires_at", "refresh_expires_at"]


def authorize_url(state: str) -> str:
    params = {
        "client_id": settings.QBO_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": settings.QBO_REDIRECT_URI,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def valid_realm_id(value: str) -> bool:
    return bool(value and REALM_ID.match(value))


def exchange_code(code: str, *, realm_id: str, user) -> QboCredential:
    """Trade the callback's code for tokens and record the connected company. Nothing is
    stored if Intuit refuses or can't be reached."""
    tokens = _token_call(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.QBO_REDIRECT_URI,
        }
    )
    with transaction.atomic(durable=True):
        cred = QboCredential.objects.select_for_update().get_or_create(singleton_id=1)[0]
        _store(cred, tokens)
        cred.realm_id = realm_id
        cred.connected_at = timezone.now()
        cred.connected_by = user
        cred.last_success_at = None  # a new connection hasn't made a call yet
        cred.broken_at = None
        cred.broken_reason = ""
        cred.save()
    return cred


def _break(cred: QboCredential, reason: str) -> None:
    """Mark the locked credential broken and alert. Called inside the refresh transaction, so
    a second waiter sees it broken and never spends the dead token or alerts again."""
    from apps.notifications.models import Notification

    cred.broken_at = timezone.now()
    cred.broken_reason = reason[:255]
    cred.save(update_fields=["broken_at", "broken_reason", "updated_at"])
    Notification.notify(
        None, Notification.Kind.SYNC_FAILED, title="QuickBooks connection lost", detail=reason
    )


def _is_fresh(cred: QboCredential) -> bool:
    return bool(cred.access_expires_at and cred.access_expires_at - REFRESH_MARGIN > timezone.now())


def refresh(*, force: bool = False) -> QboCredential:
    """Refresh the access token and store the rotated refresh token.

    One durable transaction holds the row lock across the freshness check, the call to Intuit
    and the write, so concurrent callers queue and the later ones reuse the result
    (`force=False`). `force=True` refreshes even if the token looks fresh — the 401 path.
    """
    broken = None
    with transaction.atomic(durable=True):
        cred = QboCredential.objects.select_for_update().filter(singleton_id=1).first()
        if cred is None or not cred.is_live:
            raise QboNotConnected("QuickBooks isn't connected.")
        if not force and _is_fresh(cred):
            return cred
        try:
            current = cred.refresh_token
        except InvalidToken:
            _break(cred, _BROKEN_KEY)
            broken = _BROKEN_KEY
        if broken is None:
            try:
                tokens = _token_call({"grant_type": "refresh_token", "refresh_token": current})
            except QboAPIError as exc:
                if exc.status in (400, 401) and "invalid_grant" in exc.body:
                    broken = _BROKEN_GRANT
                elif exc.status in (400, 401) and "invalid_client" in exc.body:
                    broken = _BROKEN_CLIENT
                else:
                    raise  # an outage or network failure: retry later, the connection is fine
                _break(cred, broken)
            else:
                _store(cred, tokens)
                cred.save(update_fields=[*_TOKEN_FIELDS, "updated_at"])
                return cred
    raise QboNotConnected(broken)


def ensure_fresh() -> QboCredential:
    """The live credential with an access token good for at least REFRESH_MARGIN."""
    cred = QboCredential.objects.filter(singleton_id=1).first()
    if cred is None or not cred.is_live or not enabled():
        raise QboNotConnected("QuickBooks isn't connected.")
    return cred if _is_fresh(cred) else refresh()


def _retry_after(resp) -> int:
    try:
        return max(0, int((getattr(resp, "headers", None) or {}).get("Retry-After", 1)))
    except (TypeError, ValueError):
        return 1


def request(
    method: str, path: str, *, params: dict | None = None, json: dict | None = None
) -> dict:
    """An authenticated accounting API call: `{base}/v3/company/{realm}/{path}`. Refreshes a
    near-expired token first; on a 401 refreshes once and retries once; on a 429 waits once
    (at most MAX_RATE_LIMIT_WAIT seconds) and retries once, then raises QboRateLimited."""
    cred = ensure_fresh()
    try:
        token = cred.access_token
    except InvalidToken:
        token = refresh(force=True).access_token  # unreadable → marks it broken and raises
    base = API_BASE.get(settings.QBO_ENVIRONMENT, API_BASE["sandbox"])
    url = f"{base}/v3/company/{cred.realm_id}/{path.lstrip('/')}"
    query = {"minorversion": MINOR_VERSION, **(params or {})}

    def call(bearer: str):
        return _http(
            method,
            url,
            params=query,
            json=json,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )

    resp = call(token)
    if resp.status_code == 401:
        token = refresh(force=True).access_token
        resp = call(token)
    if resp.status_code == 429:
        wait = _retry_after(resp)
        time.sleep(min(wait, MAX_RATE_LIMIT_WAIT))
        resp = call(token)
        if resp.status_code == 429:
            raise QboRateLimited(resp.text, _retry_after(resp))
    if not resp.ok:
        raise QboAPIError(resp.status_code, resp.text)
    QboCredential.objects.filter(singleton_id=1).update(last_success_at=timezone.now())
    return resp.json()


def company_info() -> dict:
    cred = ensure_fresh()
    return request("GET", f"companyinfo/{cred.realm_id}").get("CompanyInfo", {})


def disconnect() -> None:
    """Revoke at Intuit (best effort) and clear the stored connection either way. Takes the
    row lock so a refresh finishing at the same moment can't write the revoked tokens back."""
    with transaction.atomic(durable=True):
        cred = QboCredential.objects.select_for_update().filter(singleton_id=1).first()
        if cred is None:
            return
        try:
            token = cred.refresh_token
        except InvalidToken:
            token = ""  # unreadable (secret key changed): nothing we can revoke
        cred.set_tokens(access="", refresh="", access_expires_at=None, refresh_expires_at=None)
        cred.realm_id = ""
        cred.broken_at = None
        cred.broken_reason = ""
        cred.save()
    if token and enabled():
        try:
            resp = _http(
                "POST",
                REVOKE_URL,
                json={"token": token},
                headers={"Authorization": _basic_auth(), "Accept": "application/json"},
            )
            if not resp.ok:
                logger.warning("QuickBooks revoke answered %s", resp.status_code)
        except QboAPIError:
            logger.warning("QuickBooks revoke failed; the local connection is cleared anyway")
