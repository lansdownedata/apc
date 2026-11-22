from django.contrib import admin
from django.http import HttpRequest

from .models import CustomerAccount, CustomerRequest


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
        "verified_email",
        "self_registered",
        "revoked_at",
        "revoked_by",
        "last_login",
        "last_seen_at",
        "history_banner_seen_at",
    )


@admin.register(CustomerRequest)
class CustomerRequestAdmin(admin.ModelAdmin):
    """Read-only, for support: which customer asked for what about which trip, and the
    Inbox message and task it became. Staff act on the task, never here."""

    list_display = ("created_at", "kind", "account", "reservation", "topics", "reason", "task")
    list_filter = ("kind",)
    list_select_related = ("account__contact", "reservation", "task")
    search_fields = ("account__contact__email", "account__contact__name", "topics", "reason")
    date_hierarchy = "created_at"

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
