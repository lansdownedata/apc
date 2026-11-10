import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST

from . import services, two_factor
from .forms import AcceptInviteForm, UserInviteForm
from .models import Department, User
from .permissions import owner_admin_required

logger = logging.getLogger(__name__)

# Per-user capability toggles shown in the Access section. Add new ones here.
CAPABILITIES = [
    (
        "can_manage_payments",
        "Manage payments",
        "Run refunds, mark-paid, and retry charges on orders.",
    ),
]


@login_required
@owner_admin_required
def user_list(request):
    """Pending accounts sort first — they are the rows that need action."""
    users = sorted(
        User.objects.select_related("invited_by"),
        key=lambda u: (u.status != User.Status.PENDING, (u.get_full_name() or u.username).lower()),
    )
    return render(
        request,
        "accounts/user_list.html",
        {
            "users": users,
            "capabilities": CAPABILITIES,
            "invite_form": UserInviteForm(),
            "roles": User.Role.choices,
            "nav": "users",
            "page_title": "Users",
        },
    )


@login_required
@owner_admin_required
def user_detail(request, pk):
    user = get_object_or_404(User, pk=pk)
    valid = {key for key, _, _ in CAPABILITIES}
    if request.method == "POST":
        if "role" in request.POST:
            try:
                services.change_user_role(
                    target=user, new_role=request.POST["role"], actor=request.user
                )
                messages.success(request, "Role updated.")
            except services.UserManagementError as exc:
                messages.error(request, str(exc))
            return redirect("user_detail", pk=user.pk)
        if "phone_form" in request.POST:
            try:
                services.change_user_phone(
                    target=user, phone=request.POST.get("phone", ""), actor=request.user
                )
                messages.success(request, "Mobile number updated.")
            except services.UserManagementError as exc:
                messages.error(request, str(exc))
            return redirect("user_detail", pk=user.pk)
        if "departments_form" in request.POST:
            services.set_departments(user, request.POST.getlist("departments"))
            messages.success(request, "Departments updated.")
            return redirect("user_detail", pk=user.pk)
        cap = request.POST.get("capability")
        if cap in valid:
            setattr(user, cap, request.POST.get("enabled") == "on")
            user.save(update_fields=[cap])
            messages.success(request, "Access updated.")
        return redirect("user_detail", pk=user.pk)
    return render(
        request,
        "accounts/user_detail.html",
        {
            "target": user,
            "capabilities": CAPABILITIES,
            "departments": Department.choices,
            "user_departments": user.department_list,
            "nav": "users",
            "page_title": user.get_full_name() or user.username,
        },
    )


@login_required
@owner_admin_required
@require_POST
def user_invite(request):
    """Create a pending staff account and email the set-password link."""
    form = UserInviteForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
        return redirect("user_list")

    data = form.cleaned_data
    try:
        user, sent = services.invite_user(
            first_name=data["first_name"],
            last_name=data["last_name"],
            email=data["email"],
            role=data["role"],
            phone=data["phone"],
            can_manage_payments=data.get("can_manage_payments", False),
            actor=request.user,
        )
    except services.UserManagementError as exc:
        messages.error(request, str(exc))
        return redirect("user_list")

    if sent:
        messages.success(request, f"Invite sent to {user.email}.")
    else:
        messages.warning(
            request,
            f"{user.email} was created, but the invite email could not be sent. "
            "Use Resend invite to try again.",
        )
    return redirect("user_detail", pk=user.pk)


@login_required
@owner_admin_required
@require_POST
def user_resend_invite(request, pk):
    target = get_object_or_404(User, pk=pk)
    try:
        sent = services.resend_invite(target=target)
    except services.UserManagementError as exc:
        messages.error(request, str(exc))
        return redirect("user_detail", pk=pk)
    if sent:
        messages.success(request, f"Invite resent to {target.email}.")
    else:
        messages.warning(request, "The invite email could not be sent — see the server log.")
    return redirect("user_detail", pk=pk)


@login_required
@owner_admin_required
@require_POST
def user_revoke_invite(request, pk):
    target = get_object_or_404(User, pk=pk)
    try:
        services.revoke_invite(target=target)
    except services.UserManagementError as exc:
        messages.error(request, str(exc))
        return redirect("user_detail", pk=pk)
    messages.success(request, "Invite revoked.")
    return redirect("user_list")


@login_required
@owner_admin_required
@require_POST
def user_set_active(request, pk):
    target = get_object_or_404(User, pk=pk)
    active = request.POST.get("active") == "1"
    try:
        services.set_user_active(target=target, active=active, actor=request.user)
    except services.UserManagementError as exc:
        messages.error(request, str(exc))
        return redirect("user_detail", pk=pk)
    messages.success(request, "Access updated.")
    return redirect("user_detail", pk=pk)


def accept_invite(request, uidb64: str, token: str):
    """Set-password page for an invited user.

    Deliberately unauthenticated — the visitor has no password yet. The signed,
    expiring token is the protection. Setting a password changes the hash the token
    is derived from, so the link dies on use without any bookkeeping of our own.
    """
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (User.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        # 400, not 404: the route matched — it is the supplied credential that is bad.
        return render(request, "accounts/accept_invite_invalid.html", status=400)

    if request.method == "POST":
        form = AcceptInviteForm(user, request.POST)
        if form.is_valid():
            form.save()
            user.invite_accepted_at = timezone.now()
            user.save(update_fields=["invite_accepted_at"])
            messages.success(request, "Password set — you can sign in now.")
            return redirect("login")
    else:
        form = AcceptInviteForm(user)

    return render(request, "accounts/accept_invite.html", {"form": form, "target": user})


# --- Staff sign-in with a texted code -------------------------------------------------------

_WAIT_KEY = "two_factor_wait_staff"  # seconds until the next code, shown once on the code page
_BAD_SIGN_IN = "That username and password don't match. Please try again."
_VERDICT_ERRORS = {
    two_factor.Verdict.WRONG: "That code isn't right. Check it and try again.",
    two_factor.Verdict.EXPIRED: "That code has expired. Request a new one.",
    two_factor.Verdict.LOCKED: "Too many tries. Request a new code.",
    two_factor.Verdict.MISSING: "Too many tries. Request a new code.",
}


def _auth_backend() -> str:
    backends = getattr(settings, "AUTHENTICATION_BACKENDS", None)
    return backends[0] if backends else "django.contrib.auth.backends.ModelBackend"


def _send(request: HttpRequest, subject: two_factor.Subject, *, prefer: str = "sms"):
    """Send a code, remembering a resend-limit wait for the code page. Returns the Delivery,
    or None when the limit refused it (no new code exists then)."""
    try:
        delivery = two_factor.send_code(subject, prefer=prefer)
    except two_factor.ResendLimited as exc:
        request.session[_WAIT_KEY] = exc.retry_in
        return None
    request.session.pop(_WAIT_KEY, None)
    if not delivery.sent:
        logger.error("sign-in code for user %s could not be sent by text or email", subject.pk)
    return delivery


class StaffLoginView(LoginView):
    """Django's login form, except a correct password only opens the code step — unless the
    switch is off or this browser is remembered for the account."""

    template_name = "registration/login.html"

    def form_valid(self, form: AuthenticationForm) -> HttpResponse:
        user = form.get_user()
        subject = two_factor.staff_subject(user)
        if not two_factor.enabled() or two_factor.is_remembered(self.request, subject):
            two_factor.clear(self.request, "staff")
            return super().form_valid(form)

        earlier = two_factor.pending(self.request, "staff")
        delivery = _send(self.request, subject)
        if delivery is None:
            # The limit refused a new code, so the one already sent is still the live one.
            same = earlier is not None and earlier.pk == user.pk
            delivery = two_factor.Delivery(
                earlier.channel if same else "", earlier.fell_back if same else False
            )
        two_factor.begin(self.request, subject, next_url=self.get_redirect_url(), delivery=delivery)
        return redirect("login_code")


def _pending_user(request: HttpRequest) -> tuple[two_factor.Pending | None, User | None]:
    """The half-signed-in staff account, re-read so a mid-flow deactivation counts."""
    marker = two_factor.pending(request, "staff")
    if marker is None:
        return None, None
    return marker, User.objects.filter(pk=marker.pk, is_active=True).first()


def _refuse(request: HttpRequest) -> HttpResponse:
    two_factor.clear(request, "staff")
    messages.error(request, _BAD_SIGN_IN)
    return redirect("login")


def _safe_next(request: HttpRequest, url: str) -> str:
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return resolve_url(settings.LOGIN_REDIRECT_URL)


def _delivery_line(marker: two_factor.Pending, user: User) -> str:
    if marker.channel == "sms":
        return (
            f"We texted a 6-digit code to the number ending in {two_factor.mask_phone(user.phone)}."
        )
    email = two_factor.mask_email(user.email)
    if marker.channel == "email" and marker.fell_back:
        return f"The text didn't go through, so we emailed the code to {email}."
    if marker.channel == "email":
        return f"We emailed a 6-digit code to {email}."
    return "We couldn't send your code. Try again in a moment."


def login_code(request: HttpRequest) -> HttpResponse:
    """Second sign-in step: the code that was texted (or emailed) after the password."""
    marker, user = _pending_user(request)
    if marker is None:
        if request.user.is_authenticated:
            return redirect(settings.LOGIN_REDIRECT_URL)
        return redirect("login")
    if request.user.is_authenticated and request.user.pk == marker.pk:
        two_factor.clear(request, "staff")
        return redirect(settings.LOGIN_REDIRECT_URL)
    if user is None:
        return _refuse(request)

    error = ""
    wait = request.session.pop(_WAIT_KEY, None)
    if wait:
        error = f"You can request another code in {wait} seconds."

    if request.method == "POST":
        verdict = two_factor.verify(two_factor.staff_subject(user), request.POST.get("code", ""))
        if verdict is two_factor.Verdict.OK:
            auth_login(request, user, backend=_auth_backend())
            two_factor.clear(request, "staff")
            response = redirect(_safe_next(request, marker.next_url))
            if request.POST.get("remember"):
                two_factor.remember(response, two_factor.staff_subject(user))
            return response
        error = _VERDICT_ERRORS[verdict]

    return render(
        request,
        "registration/login_code.html",
        {
            "delivery_line": "" if wait and not marker.channel else _delivery_line(marker, user),
            "sent": bool(marker.channel),
            "error": error,
            "has_phone": bool(user.phone),
            "next": marker.next_url,
        },
    )


@require_POST
def login_code_resend(request: HttpRequest) -> HttpResponse:
    """Send a fresh code by text, or by email when asked; the limits apply."""
    marker, user = _pending_user(request)
    if marker is None:
        return redirect("login")
    if user is None:
        return _refuse(request)
    prefer = "email" if request.POST.get("channel") == "email" else "sms"
    delivery = _send(request, two_factor.staff_subject(user), prefer=prefer)
    if delivery is not None:
        two_factor.note_delivery(request, "staff", delivery)
    return redirect("login_code")
