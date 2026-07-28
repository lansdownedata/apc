from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from .forms import AcceptInviteForm
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
