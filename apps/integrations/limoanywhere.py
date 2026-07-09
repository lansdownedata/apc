"""LimoAnywhere Customer API client (reference: docs/la-api/).

Two OAuth2 grants: client_credentials (register customers, company resources)
and per-customer password grant (rate lookup, bookings, reservations, webhook
subscriptions). Orchestration/ORM work lives in la_sync.py, not here.
"""

import time

import requests
from django.conf import settings

TIMEOUT = 30
_TOKEN_EXPIRY_BUFFER = 60
_token_cache: dict[str, tuple[str, float]] = {}


class LANotConfigured(Exception):
    """LA credentials are not set (the app runs in preview mode)."""


class LAAPIError(Exception):
    """A non-2xx response from the LA API (carries status + body)."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"LimoAnywhere API {status_code}: {body[:600]}")


def is_configured() -> bool:
    return bool(settings.LA_CLIENT_ID and settings.LA_CLIENT_SECRET and settings.LA_COMPANY_ALIAS)


def get_token(*, username: str = "", password: str = "") -> str:
    """Bearer token — client-credentials by default, password grant when username given."""
    if not is_configured():
        raise LANotConfigured("Set LA_CLIENT_ID / LA_CLIENT_SECRET / LA_COMPANY_ALIAS in .env.")
    cache_key = username or "__client__"
    cached = _token_cache.get(cache_key)
    if cached and cached[1] - _TOKEN_EXPIRY_BUFFER > time.time():
        return cached[0]
    data = {
        "grant_type": "password" if username else "client_credentials",
        "client_id": settings.LA_CLIENT_ID,
        "client_secret": settings.LA_CLIENT_SECRET,
    }
    if username:
        data |= {"username": username, "password": password}
    resp = requests.post(f"{settings.LA_BASE_URL}/oauth2/token", data=data, timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise LAAPIError(resp.status_code, resp.text or "")
    payload = resp.json()
    token = payload["access_token"]
    _token_cache[cache_key] = (token, time.time() + int(payload.get("expires_in", 300)))
    return token


def _request(method: str, path: str, *, token: str, json=None, params=None) -> dict:
    resp = requests.request(
        method,
        f"{settings.LA_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        json=json,
        params=params,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise LAAPIError(resp.status_code, resp.text or "")
    return resp.json() if resp.content else {}


def _alias() -> str:
    return settings.LA_COMPANY_ALIAS


def register_customer(payload: dict) -> dict:
    return _request(
        "POST", f"/companies/{_alias()}/customers/sign_up", token=get_token(), json=payload
    )


def validate_email(email: str) -> dict:
    return _request(
        "POST",
        f"/companies/{_alias()}/customers/emails/validate",
        token=get_token(),
        json={"email": email},
    )


def list_payment_types() -> dict:
    return _request("GET", f"/companies/{_alias()}/resources/payment_types", token=get_token())


def list_service_types() -> dict:
    return _request("GET", f"/companies/{_alias()}/resources/service_types", token=get_token())


def list_vehicle_types() -> dict:
    return _request("GET", f"/companies/{_alias()}/resources/vehicle_types", token=get_token())


def rate_lookup(payload: dict, *, token: str) -> dict:
    return _request("POST", f"/companies/{_alias()}/rate_lookup", token=token, json=payload)


def create_booking(payload: dict, *, token: str) -> dict:
    return _request("POST", f"/companies/{_alias()}/bookings", token=token, json=payload)


def get_reservation(reservation_id: str, *, token: str) -> dict:
    return _request("GET", f"/customers/self/reservations/{reservation_id}", token=token)


def cancel_reservation(reservation_id: str, *, token: str) -> dict:
    return _request("POST", f"/customers/self/reservations/{reservation_id}/cancel", token=token)


def subscribe_webhook(uri: str, *, token: str) -> None:
    _request("PUT", "/customers/self/subscriptions/webhook", token=token, json={"uri": uri})
