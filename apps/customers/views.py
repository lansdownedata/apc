"""The customer portal, `/account/`: sign in, sign up, passwords, and the pages behind them.

Never `@login_required`: customers have their own session (`apps.customers.auth`) and never
set `request.user`. Modelled on `apps/planners/views.py`, without the texted code: a
customer signs in with a password and a verified email.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit

from django.contrib import messages
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.core.net import client_ip
from apps.planners.selectors import dispatch_phone

from . import selectors, services
from .auth import customer_required, sign_in, sign_out
from .forms import CustomerPasswordForm, SignupForm
from .models import CustomerAccount
from .tokens import customer_reset_token, customer_token

# Per-IP brakes, as on the planner sign-in (`apps/planners/views.py`). Per-process cache
# (LocMem): a brake on guessing, not a lock. Sign-up mails count against the reset limit.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 15 * 60
RESET_LIMIT = 5
RESET_WINDOW = 60 * 60

# Like Django's PasswordResetConfirmView: a link's token is moved into the session and the
# address bar shows this placeholder instead, so it never sits in history or a Referer.
TOKEN_PLACEHOLDER = "set"
LINK_SESSION_KEY = "_customer_link_token"
SIGNUP_SESSION_KEY = "_customer_signup_token"


def _page(
    request: HttpRequest, template: str, context: dict | None = None, status: int = 200
) -> HttpResponse:
    """Render a customer page. The shell shows the dispatch number on every one, and the
    portal nav only behind `customer_required`."""
    context = {
        "dispatch": dispatch_phone(),
        "signed_in": hasattr(request, "customer"),
        **(context or {}),
    }
    return render(request, template, context, status=status)


def _over(request: HttpRequest, scope: str, limit: int) -> bool:
    return cache.get(f"customer-{scope}:{client_ip(request)}", 0) >= limit


def _count(request: HttpRequest, scope: str, window: int) -> None:
    key = f"customer-{scope}:{client_ip(request)}"
    cache.set(key, cache.get(key, 0) + 1, window)


def _home() -> str:
    return reverse("customers:home")


def _safe_next(request: HttpRequest) -> str:
    """`?next=` when it is a same-host path under `/account/`; Home otherwise.

    The path is checked as a browser will resolve it: percent-decoded (`%2e` is `.`), `\\`
    read as `/`, then `..` applied, so `/account/%2e%2e/portal/` is refused."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    path = unquote(urlsplit(target).path).replace("\\", "/")
    path = posixpath.normpath(path) + "/"
    if (
        target.startswith(_home())
        and path.startswith(_home())
        and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
    ):
        return target
    return _home()


def _link_invalid(request: HttpRequest, message: str = "") -> HttpResponse:
    return _page(request, "customers/link_invalid.html", {"message": message}, status=400)


# --- sign in / out ---------------------------------------------------------------------


@sensitive_post_parameters("password")
def login(request: HttpRequest) -> HttpResponse:
    context = {"next": request.POST.get("next") or request.GET.get("next") or ""}
    if request.method == "POST":
        if _over(request, "login", LOGIN_FAILURE_LIMIT):
            return _page(request, "customers/login.html", {**context, "throttled": True}, 429)
        email = request.POST.get("email", "")
        account = services.authenticate(email, request.POST.get("password", ""))
        if account is not None:
            sign_in(request, account)
            return redirect(_safe_next(request))
        _count(request, "login", LOGIN_FAILURE_WINDOW)
        context.update(error=True, email=email)
    return _page(request, "customers/login.html", context)


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    sign_out(request)
    messages.success(request, "You're signed out.")
    return redirect("customers:login")


# --- sign up ---------------------------------------------------------------------------


def signup(request: HttpRequest) -> HttpResponse:
    """Step 1, then step 2 ("Check your email") with the typed email, whatever happened to
    it. Only a site-wide refusal (sending off, no public address) shows here instead."""
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if not _over(request, "reset", RESET_LIMIT):
            _count(request, "reset", RESET_WINDOW)
            try:
                services.start_signup(name=data["name"], email=data["email"], phone=data["phone"])
            except services.CustomerError as exc:
                return _page(request, "customers/signup.html", {"form": form, "error": str(exc)})
        return _page(
            request,
            "customers/signup_sent.html",
            {
                "email": data["email"].strip().lower(),
                "form": form,
                "link_days": services.SIGNUP_MAX_AGE.days,
            },
        )
    return _page(request, "customers/signup.html", {"form": form})


def _hop(request: HttpRequest, session_key: str, token: str, url: str) -> HttpResponse:
    request.session[session_key] = token
    return redirect(url)


@sensitive_post_parameters("new_password1", "new_password2")
def signup_confirm(request: HttpRequest, token: str) -> HttpResponse:
    """Step 3 from a sign-up link: choose a password, then the account exists and the
    customer is signed in."""
    if token != TOKEN_PLACEHOLDER:
        try:
            services.read_signup(token)
        except signing.BadSignature:
            return _link_invalid(request)
        url = reverse("customers:signup_confirm", args=[TOKEN_PLACEHOLDER])
        return _hop(request, SIGNUP_SESSION_KEY, token, url)
    try:
        data = services.read_signup(request.session.get(SIGNUP_SESSION_KEY, ""))
    except signing.BadSignature:
        return _link_invalid(request)
    form = CustomerPasswordForm(None, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            account = services.complete_signup(data, form.cleaned_data["new_password1"])
        except services.CustomerError as exc:
            return _link_invalid(request, str(exc))
        request.session.pop(SIGNUP_SESSION_KEY, None)
        sign_in(request, account)
        messages.success(request, "You're all set.")
        return redirect(_home())
    return _page(
        request,
        "customers/set_password.html",
        {"form": form, "email": data.get("email", ""), "signup": True},
    )


# --- set a password from an emailed link -----------------------------------------------


def _account_from_link(
    uidb64: str, token: str, generator: PasswordResetTokenGenerator
) -> CustomerAccount | None:
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        account = CustomerAccount.objects.select_related("contact").get(pk=pk)
    except (CustomerAccount.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError):
        return None
    if account.status == CustomerAccount.Status.REVOKED:
        return None
    return account if generator.check_token(account, token) else None


@sensitive_post_parameters("new_password1", "new_password2")
def set_password(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    """The invite (`register`) and password-reset (`set_password`) links share this page.
    Unauthenticated by design: the token is the guard, and it dies once a password is set."""
    name = request.resolver_match.url_name
    generator = customer_reset_token if name == "set_password" else customer_token
    if token != TOKEN_PLACEHOLDER:
        if _account_from_link(uidb64, token, generator) is None:
            return _link_invalid(request)
        url = reverse(f"customers:{name}", kwargs={"uidb64": uidb64, "token": TOKEN_PLACEHOLDER})
        return _hop(request, LINK_SESSION_KEY, token, url)
    account = _account_from_link(uidb64, request.session.get(LINK_SESSION_KEY, ""), generator)
    if account is None:
        return _link_invalid(request)
    first_time = account.registered_at is None
    form = CustomerPasswordForm(account, request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.register(account, form.cleaned_data["new_password1"])
        request.session.pop(LINK_SESSION_KEY, None)
        sign_in(request, account)
        messages.success(request, "You're all set." if first_time else "Password updated.")
        return redirect(_home())
    return _page(
        request,
        "customers/set_password.html",
        {"form": form, "email": account.email, "reset": not first_time},
    )


def password_reset(request: HttpRequest) -> HttpResponse:
    """Forgot password. The same answer whoever asked, so it never confirms an account."""
    email = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        if not _over(request, "reset", RESET_LIMIT):
            _count(request, "reset", RESET_WINDOW)
            services.send_password_reset(email)
    return _page(
        request,
        "customers/password_reset.html",
        {
            "sent": request.method == "POST",
            "email": email,
            "link_days": customer_reset_token.timeout.days,
        },
    )


# --- signed in -------------------------------------------------------------------------


@customer_required
def home(request: HttpRequest) -> HttpResponse:
    """Next trip, what is waiting on the customer, what is coming up. The history banner
    shows once: the first time it has something to say, it is stamped as seen."""
    account = request.customer
    context = selectors.home(account)
    if context["history_banner"] is not None:
        account.history_banner_seen_at = timezone.now()
        account.save(update_fields=["history_banner_seen_at"])
    return _page(request, "customers/home.html", context)
