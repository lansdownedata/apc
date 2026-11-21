"""Customer sign-in, kept apart from Django's staff authentication.

A customer session is two keys in the ordinary session, the account id and an HMAC of its
password hash, and nothing else. `django.contrib.auth` never hears about it: no
`_auth_user_id`, no `request.user`. Every staff view is `@login_required`, and to it a
signed-in customer is an anonymous visitor; a staff session, in turn, never passes
`customer_required`.

`customer_required` re-reads the account on every request, so Revoke, a new password or a
cleared contact email take effect on the very next page load.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from urllib.parse import quote

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import CustomerAccount

SESSION_KEY = "customer_account_id"
HASH_SESSION_KEY = "customer_auth_hash"


def _session_hash(account: CustomerAccount) -> str:
    return salted_hmac(
        "apps.customers.auth.session", account.password, algorithm="sha256"
    ).hexdigest()


def sign_in(request: HttpRequest, account: CustomerAccount) -> None:
    """Start a customer session. The session key is rotated (fixation), but a staff or
    planner login in the same browser is left alone: none of them share keys."""
    request.session.cycle_key()
    request.session[SESSION_KEY] = account.pk
    request.session[HASH_SESSION_KEY] = _session_hash(account)
    account.last_login = timezone.now()
    account.save(update_fields=["last_login"])


def sign_out(request: HttpRequest) -> None:
    for key in (SESSION_KEY, HASH_SESSION_KEY):
        request.session.pop(key, None)
    request.session.cycle_key()


def current_customer(request: HttpRequest) -> CustomerAccount | None:
    """The signed-in customer, or None. A session whose account was revoked, whose
    password changed since sign-in, or whose contact lost its email is ended here."""
    pk = request.session.get(SESSION_KEY)
    if pk is None:
        return None
    account = CustomerAccount.objects.select_related("contact").filter(pk=pk).first()
    stored = request.session.get(HASH_SESSION_KEY, "")
    if (
        account is None
        or not account.can_sign_in
        or not constant_time_compare(stored, _session_hash(account))
    ):
        sign_out(request)
        return None
    return account


def _log_view(account: CustomerAccount) -> None:
    now = timezone.now()
    CustomerAccount.objects.filter(pk=account.pk).update(last_seen_at=now)
    account.last_seen_at = now


def customer_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Only a signed-in customer gets through; everyone else (a staff session included) is
    sent to the customer sign-in. Sets `request.customer`; never reads `request.user`."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        account = current_customer(request)
        if account is None:
            nxt = quote(request.get_full_path(), safe="/")
            return redirect(f"{reverse('customers:login')}?next={nxt}")
        request.customer = account
        if request.method == "GET":
            _log_view(account)
        return view(request, *args, **kwargs)

    return wrapped
