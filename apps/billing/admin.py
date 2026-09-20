from django.contrib import admin

from .models import AccountGroup, BillingAccount


class AccountGroupInline(admin.TabularInline):
    model = AccountGroup
    extra = 0
    fields = ("name", "invoice_email", "terms", "po_required", "is_default", "qbo_sync_state")


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "contact", "terms", "qbo_sync_state", "created_at")
    list_filter = ("terms", "qbo_sync_state")
    search_fields = ("name", "contact__name", "qbo_customer_id")
    autocomplete_fields = ("contact",)
    inlines = [AccountGroupInline]


@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "account", "invoice_email", "is_default", "qbo_sync_state")
    list_filter = ("is_default", "po_required", "qbo_sync_state")
    search_fields = ("name", "account__name", "invoice_email")
