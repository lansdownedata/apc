"""The planner area, `/planner/` (APC-66). Public shell, mobile-first, its own sign-in.

Nothing here is reachable with a staff session and nothing in the staff portal is reachable
with this one — see `apps.planners.auth`.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.accounts import two_factor
from apps.accounts.forms import AcceptInviteForm
from apps.core.net import client_ip

from . import selectors, services
from .auth import current_planner, planner_required, planner_subject, sign_in, sign_out
from .models import PlannerAccount
from .tokens import planner_reset_token, planner_token

# Failed sign-ins per IP before the form stops answering for a while. Per-process cache
# (LocMem), like the public site's other throttles — a brake on guessing, not a lock.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW = 15 * 60
RESET_LIMIT = 5
RESET_WINDOW = 60 * 60
CODE_LIMIT = 10  # sign-in codes typed per IP, right or wrong, before the code page rests
CODE_WINDOW = 15 * 60

logger = logging.getLogger(__name__)


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
    return _safe_url(request, request.POST.get("next") or request.GET.get("next") or "")


def _safe_url(request, target: str) -> str:
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("planners:weddings")


def login(request: HttpRequest) -> HttpResponse:
    context = {"next": request.POST.get("next") or request.GET.get("next") or ""}
    if request.session.pop(_TIMED_OUT_KEY, False):
        context["timed_out"] = True
    if request.method == "POST":
        if _over(request, "login", LOGIN_FAILURE_LIMIT):
            return _page(request, "planners/login.html", {**context, "throttled": True}, 429)
        email = request.POST.get("email", "")
        account = services.authenticate(email, request.POST.get("password", ""))
        if account is not None:
            return _finish_sign_in(request, account, next_url=_safe_next(request))
        _count(request, "login", LOGIN_FAILURE_WINDOW)
        context.update(error=True, email=email)
    return _page(request, "planners/login.html", context)


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    sign_out(request)
    messages.success(request, "You're signed out.")
    return redirect("planners:login")


def _account_from_link(uidb64: str, token: str, generator) -> PlannerAccount | None:
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        account = PlannerAccount.objects.select_related("contact").get(pk=pk)
    except (PlannerAccount.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError):
        return None
    if account.status == PlannerAccount.Status.REVOKED:
        return None
    return account if generator.check_token(account, token) else None


# Like Django's PasswordResetConfirmView: the token is moved into the session and the
# address bar shows this placeholder instead, so it never sits in history or a Referer.
TOKEN_PLACEHOLDER = "set"
LINK_SESSION_KEY = "_planner_link_token"


def set_password(request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
    """Registration (from the invite) and password reset share this page: both are a
    signed link that lets the holder choose a password. Unauthenticated by design — the
    token is the guard, and it dies the moment a password is set."""
    name = request.resolver_match.url_name
    generator = planner_reset_token if name == "set_password" else planner_token
    if token != TOKEN_PLACEHOLDER:
        account = _account_from_link(uidb64, token, generator)
        if account is None:
            return _page(request, "planners/link_invalid.html", status=400)
        request.session[LINK_SESSION_KEY] = token
        return redirect(
            reverse(f"planners:{name}", kwargs={"uidb64": uidb64, "token": TOKEN_PLACEHOLDER})
        )
    account = _account_from_link(uidb64, request.session.get(LINK_SESSION_KEY, ""), generator)
    if account is None:
        return _page(request, "planners/link_invalid.html", status=400)
    first_time = account.registered_at is None
    form = AcceptInviteForm(account, request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.register(account, form.cleaned_data["new_password1"])
        request.session.pop(LINK_SESSION_KEY, None)
        return _finish_sign_in(
            request,
            account,
            next_url=reverse("planners:weddings"),
            flash="You're all set." if first_time else "Password updated.",
        )
    return _page(
        request,
        "planners/set_password.html",
        {"form": form, "account": account, "first_time": first_time},
    )


# --- The texted sign-in code -----------------------------------------------------------------

_WAIT_KEY = "two_factor_wait_planner"  # seconds until the next code, shown once on the code page
_TIMED_OUT_KEY = "two_factor_timed_out_planner"  # shown once on the login page
_FLASH_KEY = "two_factor_flash_planner"  # the set-password flash, held back until the code
_VERDICT_ERRORS = {
    two_factor.Verdict.WRONG: "That code isn't right. Check it and try again.",
    two_factor.Verdict.EXPIRED: "That code has expired. Request a new one.",
    two_factor.Verdict.LOCKED: "Too many tries. Request a new code.",
    two_factor.Verdict.MISSING: "Too many tries. Request a new code.",
}


def _send_code(
    request, subject: two_factor.Subject, *, prefer: str = "sms"
) -> two_factor.Delivery | None:
    """Send a code, remembering a resend-limit wait for the code page. Returns the Delivery,
    or None when the limit refused it (no new code exists then)."""
    try:
        delivery = two_factor.send_code(subject, prefer=prefer)
    except two_factor.ResendLimited as exc:
        request.session[_WAIT_KEY] = exc.retry_in
        return None
    request.session.pop(_WAIT_KEY, None)
    if not delivery.sent:
        logger.error("sign-in code for planner %s could not be sent by text or email", subject.pk)
    return delivery


def _finish_sign_in(
    request, account: PlannerAccount, *, next_url: str, flash: str = ""
) -> HttpResponse:
    """The password (or a set-password link) checked out. Sign in directly when the switch
    is off or this browser is remembered for the account; otherwise send a code and open
    the code step. `flash` is shown once the planner is actually in."""
    subject = planner_subject(account)
    if not two_factor.enabled() or two_factor.is_remembered(request, subject):
        two_factor.clear(request, "planner")
        request.session.pop(_FLASH_KEY, None)
        sign_in(request, account)
        if flash:
            messages.success(request, flash)
        return redirect(next_url)

    earlier = two_factor.pending(request, "planner")
    delivery = _send_code(request, subject)
    if delivery is None:
        # The limit refused a new code, so the one already sent is still the live one.
        same = earlier is not None and earlier.pk == account.pk
        delivery = two_factor.Delivery(
            earlier.channel if same else "", earlier.fell_back if same else False
        )
    two_factor.begin(request, subject, next_url=next_url, delivery=delivery)
    if flash:
        request.session[_FLASH_KEY] = flash
    else:
        request.session.pop(_FLASH_KEY, None)
    return redirect("planners:login_code")


def _pending_account(request) -> tuple[two_factor.Pending | None, PlannerAccount | None]:
    """The half-signed-in planner, re-read so a mid-flow revoke counts."""
    marker = two_factor.pending(request, "planner")
    if marker is None:
        return None, None
    account = PlannerAccount.objects.select_related("contact").filter(pk=marker.pk).first()
    return marker, account if account is not None and account.can_sign_in else None


def _refuse(request, marker: two_factor.Pending) -> HttpResponse:
    two_factor.clear(request, "planner")
    request.session.pop(_FLASH_KEY, None)
    return _page(request, "planners/login.html", {"error": True, "next": marker.next_url})


def _delivery_line(marker: two_factor.Pending, account: PlannerAccount) -> str:
    if marker.channel == "sms":
        return (
            "We texted a 6-digit code to the number ending in "
            f"{two_factor.mask_phone(account.phone)}."
        )
    email = two_factor.mask_email(account.email)
    if marker.channel == "email" and marker.fell_back:
        return f"The text didn't go through, so we emailed the code to {email}."
    if marker.channel == "email":
        return f"We emailed a 6-digit code to {email}."
    return "We couldn't send your code. Try again in a moment."


def _no_marker(request: HttpRequest) -> HttpResponse:
    """Back to the password step; the login page says so when the visitor's sign-in timed out."""
    if two_factor.take_expired(request, "planner"):
        request.session[_TIMED_OUT_KEY] = True
    return redirect("planners:login")


@sensitive_post_parameters("code")
@never_cache
def login_code(request: HttpRequest) -> HttpResponse:
    """Second sign-in step: the code that was texted (or emailed) after the password."""
    marker, account = _pending_account(request)
    signed_in = current_planner(request)
    if marker is None:
        if signed_in:
            return redirect("planners:weddings")
        return _no_marker(request)
    if signed_in is not None and signed_in.pk == marker.pk:
        two_factor.clear(request, "planner")
        return redirect("planners:weddings")
    if account is None:
        return _refuse(request, marker)

    error, status, throttled = "", 200, False
    wait = request.session.pop(_WAIT_KEY, None)
    if wait:
        error = f"You can request another code in {wait} seconds."

    if request.method == "POST":
        if _over(request, "code", CODE_LIMIT):
            error, status, throttled = "", 429, True
        else:
            subject = planner_subject(account)
            verdict = two_factor.verify(subject, request.POST.get("code", ""))
            if verdict is two_factor.Verdict.OK:
                sign_in(request, account)
                two_factor.clear(request, "planner")
                flash = request.session.pop(_FLASH_KEY, "")
                if flash:
                    messages.success(request, flash)
                response = redirect(_safe_url(request, marker.next_url))
                if request.POST.get("remember"):
                    two_factor.remember(response, subject)
                return response
            _count(request, "code", CODE_WINDOW)
            error = _VERDICT_ERRORS[verdict]

    return _page(
        request,
        "planners/login_code.html",
        {
            "delivery_line": "" if wait and not marker.channel else _delivery_line(marker, account),
            "error": error,
            "throttled": throttled,
            "has_phone": bool(account.phone),
            "next": marker.next_url,
        },
        status,
    )


@never_cache
@require_POST
def login_code_resend(request: HttpRequest) -> HttpResponse:
    """Send a fresh code by text, or by email when asked; the limits apply."""
    marker, account = _pending_account(request)
    if marker is None:
        return _no_marker(request)
    if account is None:
        return _refuse(request, marker)
    prefer = "email" if request.POST.get("channel") == "email" else "sms"
    delivery = _send_code(request, planner_subject(account), prefer=prefer)
    if delivery is not None:
        two_factor.note_delivery(request, "planner", delivery)
    return redirect("planners:login_code")


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


CHANGE_STATES = ("form", "sent", "limited")


@planner_required
def wedding(request: HttpRequest, pk: int) -> HttpResponse:
    lead = _wedding_or_404(request, pk)
    change = request.GET.get("change", "")
    return _page(
        request,
        "planners/wedding.html",
        {
            "w": selectors.dashboard(lead),
            "planner": request.planner,
            "change": change if change in CHANGE_STATES else "",
            "about": request.GET.get("about", ""),
            "request_limit_reached": services.over_limit(request.planner),
        },
    )


@planner_required
@require_POST
def request_change(request: HttpRequest, pk: int) -> HttpResponse:
    """APC-68. POST + CSRF only; the planner must hold a role on the wedding (404 if not)
    and the wedding must not be archived (403) — an archive is a record, not a channel."""
    lead = _wedding_or_404(request, pk)
    w = selectors.dashboard(lead)
    if w["archived"]:
        raise PermissionDenied
    labels = dict(w["request_options"])
    value = request.POST.get("about", "")
    about = labels.get(value, "") if value else ""
    base = reverse("planners:wedding", args=[lead.pk])
    try:
        services.request_change(
            request.planner, lead, about=about, body=request.POST.get("body", "")
        )
    except services.RequestRefused as exc:
        if exc.reason == "limited":
            return redirect(f"{base}?change=limited#request")
        if exc.reason == "too_long":
            limit = f"{services.MAX_REQUEST_LENGTH:,}"
            messages.error(request, f"Please keep it under {limit} characters.")
        else:
            messages.error(request, "Tell us what needs to change, then send it.")
        return redirect(f"{base}?change=form#request")
    return redirect(f"{base}?change=sent#request")
