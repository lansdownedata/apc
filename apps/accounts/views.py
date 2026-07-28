from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.views.decorators.http import require_POST

from . import services
from .forms import AcceptInviteForm, UserInviteForm
from .models import User
from .permissions import owner_admin_required

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
    users = User.objects.order_by("role", "username")
    return render(
        request,
        "accounts/user_list.html",
        {
            "users": users,
            "capabilities": CAPABILITIES,
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
