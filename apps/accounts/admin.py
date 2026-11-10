from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import ValidationError

from apps.core.phone import to_e164

from .forms import INVALID_PHONE
from .models import User, UserDepartment


class UserAdminForm(UserChangeForm):
    """Blank stays allowed here (createsuperuser accounts start without a number), but a
    number must be a real one, stored as E.164 so sign-in codes can be texted to it."""

    class Meta(UserChangeForm.Meta):
        model = User

    def clean_phone(self) -> str:
        raw = (self.cleaned_data.get("phone") or "").strip()
        if not raw:
            return ""
        phone = to_e164(raw)
        if phone is None:
            raise ValidationError(INVALID_PHONE)
        return phone


class UserDepartmentInline(admin.TabularInline):
    model = UserDepartment
    extra = 0


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = UserAdminForm
    inlines = [UserDepartmentInline]
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Lead Manager",
            {"fields": ("role", "phone", "can_manage_payments")},
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
