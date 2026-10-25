from django.contrib import admin

from .models import PlannerAccount


@admin.register(PlannerAccount)
class PlannerAccountAdmin(admin.ModelAdmin):
    """Read-mostly: invites, resends and revocations go through the contact profile so the
    mail and the state machine stay in one place. The password hash is never shown."""

    list_display = ("email", "contact", "status", "invited_at", "registered_at", "revoked_at")
    search_fields = ("email", "contact__name")
    exclude = ("password",)
    readonly_fields = (
        "contact",
        "email",
        "invited_at",
        "invited_by",
        "registered_at",
        "revoked_at",
        "revoked_by",
        "last_login",
        "last_seen_at",
        "view_count",
    )
