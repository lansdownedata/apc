"""The planner area, `/planner/` (APC-66). Public shell, mobile-first, its own sign-in.

Nothing here is reachable with a staff session and nothing in the staff portal is reachable
with this one — see `apps.planners.auth`.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST

from apps.accounts.forms import AcceptInviteForm
from apps.core.net import client_ip

from . import selectors, services
from .auth import planner_required, sign_in, sign_out
from .models import PlannerAccount
from .tokens import planner_token

# Failed sign-ins per IP before the form stops answering for a while. Per-process cache
# (LocMem), like the public site's other throttles — a brake on guessing, not a lock.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 15 * 60
RESET_LIMIT = 5
RESET_WINDOW = 60 * 60


def _page(request, template: str, context: dict | None = None, status: int = 200):
    """Render a planner page. Every one shows the dispatch number in its header."""
    context = {"dispatch": selectors.dispatch_phone(), **(context or {})}
    return render(request, template, context, status=status)


def _over(request, scope: str, limit: int) -> bool:
    return cache.get(f"planner-{scope}:{client_ip(request)}", 0) >= limit


def _count(request, scope: str, window: int) -> None:
    key = f"planner-{scope}:{client_ip(request)}"
    cache.set(key, cache.get(key, 0) + 1, window)


def _safe_next(request) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("planners:weddings")


def login(request: HttpRequest) -> HttpResponse:
    context = {"next": request.POST.get("next") or request.GET.get("next") or ""}
    if request.method == "POST":
        if _over(request, "login", LOGIN_FAILURE_LIMIT):
            return _page(request, "planners/login.html", {**context, "throttled": True}, 429)
        email = request.POST.get("email", "")
        account = services.authenticate(email, request.POST.get("password", ""))
        if account is not None:
            sign_in(request, account)
            return redirect(_safe_next(request))
        _count(request, "login", LOGIN_FAILURE_WINDOW)
        context.update(error=True, email=email)
    return _page(request, "planners/login.html", context)


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    sign_out(request)
    messages.success(request, "You're signed out.")
    return redirect("planners:login")


def _account_from_link(uidb64: str, token: str) -> PlannerAccount | None:
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        account = PlannerAccount.objects.select_related("contact").get(pk=pk)
    except (PlannerAccount.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError):
        return None
    if account.status == PlannerAccount.Status.REVOKED:
        return None
    return account if planner_token.check_token(account, token) else None


def set_password(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    """Registration (from the invite) and password reset share this page: both are a
    signed link that lets the holder choose a password. Unauthenticated by design — the
    token is the guard, and it dies the moment a password is set."""
    account = _account_from_link(uidb64, token)
    if account is None:
        return _page(request, "planners/link_invalid.html", status=400)
    first_time = account.registered_at is None
    form = AcceptInviteForm(account, request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.register(account, form.cleaned_data["new_password1"])
        sign_in(request, account)
        messages.success(request, "You're all set." if first_time else "Password updated.")
        return redirect("planners:weddings")
    return _page(
        request,
        "planners/set_password.html",
        {"form": form, "account": account, "first_time": first_time},
    )


def password_reset(request: HttpRequest) -> HttpResponse:
    sent = False
    if request.method == "POST":
        if not _over(request, "reset", RESET_LIMIT):
            _count(request, "reset", RESET_WINDOW)
            services.send_password_reset(request.POST.get("email", ""))
        sent = True  # the same answer whoever asked, so it never confirms an account
    return _page(request, "planners/password_reset.html", {"sent": sent})


@planner_required
def weddings(request: HttpRequest) -> HttpResponse:
    current, past = selectors.wedding_list(request.planner)
    return _page(
        request,
        "planners/weddings.html",
        {"current": current, "past": past, "planner": request.planner},
    )


def _wedding_or_404(request, pk: int):
    lead = services.weddings_for(request.planner).filter(pk=pk).first()
    if lead is None:
        raise Http404
    return lead


@planner_required
def wedding(request: HttpRequest, pk: int) -> HttpResponse:
    lead = _wedding_or_404(request, pk)
    return _page(
        request,
        "planners/wedding.html",
        {"w": selectors.dashboard(lead), "planner": request.planner},
    )
