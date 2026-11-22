"""Customer sign-in, kept apart from Django's staff authentication.

A customer session is two keys in the ordinary session, the account id and an HMAC of its
password hash, and nothing else. `django.contrib.auth` never hears about it: no
`_auth_user_id`, no `request.user`. Every staff view is `@login_required`, and to it a
signed-in customer is an anonymous visitor; a staff session, in turn, never passes
`customer_required`.

`customer_required` re-reads the account on every request, so Revoke, a new password or a
changed or cleared contact email take effect on the very next page load.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from urllib.parse import quote

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import CustomerAccount

SESSION_KEY = "customer_account_id"
HASH_SESSION_KEY = "customer_auth_hash"


def _session_hash(account: CustomerAccount) -> str:
    """Covers the password and the sign-in email, so a new password or a staff change of
    the contact's email ends every session opened before it."""
    return salted_hmac(
        "apps.customers.auth.session", f"{account.email}\n{account.password}", algorithm="sha256"
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
    password or contact email changed since sign-in, or whose contact lost its email is
    ended here."""
    pk = request.session.get(SESSION_KEY)
    if pk is None:
        return None
    account = CustomerAccount.objects.select_related("contact__company").filter(pk=pk).first()
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


# A page asks for a piece of itself with `?fragment=1` (the Trips table); it is never a
# place to come back to after signing in.
FRAGMENT_PARAM = "fragment"


def _wants_json(request: HttpRequest) -> bool:
    """A page's own script asking (fetch with `X-Requested-With` or `Accept: application/json`)
    rather than the browser navigating."""
    return request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")


def return_path(request: HttpRequest) -> str:
    """The request's path and query without `fragment`: the whole page to come back to."""
    query = request.GET.copy()
    query.pop(FRAGMENT_PARAM, None)
    return f"{request.path}?{query.urlencode()}" if query else request.path


def customer_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Only a signed-in customer gets through; everyone else (a staff session included) is
    sent to the customer sign-in, or, when the page's own script asked, answered 401 so it
    can reload the whole page instead of following a redirect into a piece of one. Sets
    `request.customer`; never reads `request.user`."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        account = current_customer(request)
        if account is None:
            if _wants_json(request):
                return JsonResponse({"ok": False, "error": "signed_out"}, status=401)
            nxt = quote(return_path(request), safe="/")
            return redirect(f"{reverse('customers:login')}?next={nxt}")
        request.customer = account
        if request.method == "GET":
            _log_view(account)
        return view(request, *args, **kwargs)

    return wrapped
