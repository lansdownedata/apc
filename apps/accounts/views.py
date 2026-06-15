from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

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
