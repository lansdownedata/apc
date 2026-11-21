from django.contrib import admin

from .models import CustomerAccount


@admin.register(CustomerAccount)
class CustomerAccountAdmin(admin.ModelAdmin):
    """Read-mostly: invites, resends and revocations go through the contact profile so the
    mail and the state machine stay in one place. The password hash is never shown."""

    list_display = ("email", "contact", "status", "invited_at", "registered_at", "revoked_at")
    list_select_related = ("contact",)
    search_fields = ("contact__email", "contact__name")
    exclude = ("password",)
    readonly_fields = (
        "contact",
        "invited_at",
        "invited_by",
        "registered_at",
        "self_registered",
        "revoked_at",
        "revoked_by",
        "last_login",
        "last_seen_at",
        "history_banner_seen_at",
    )
