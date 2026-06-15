from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Lead Manager",
            {"fields": ("role", "phone", "two_factor_enabled", "can_manage_payments")},
        ),
    )
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "is_staff",
        "can_manage_payments",
    )
    list_filter = (*DjangoUserAdmin.list_filter, "role")
