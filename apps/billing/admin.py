from django.contrib import admin

from .models import (
    AccountGroup,
    BillingAccount,
    Invoice,
    InvoiceLine,
    QboAccount,
    QboAccountMapping,
    QboItem,
    QboTerm,
    SyncState,
)

# Fields QuickBooks holds a copy of (APC-42): changing one here queues the record again, the
# same as an edit on the contact page does.
_SYNCED_FIELDS = {"name", "terms", "invoice_email", "account"}


def _requeue_if_changed(obj, changed: set[str]) -> bool:
    if changed & _SYNCED_FIELDS:
        obj.qbo_sync_state = SyncState.NOT_SYNCED
        return True
    return False


class AccountGroupInline(admin.TabularInline):
    model = AccountGroup
    extra = 0
    fields = ("name", "invoice_email", "terms", "po_required", "is_default", "qbo_sync_state")


@admin.register(BillingAccount)
class BillingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "contact", "terms", "qbo_sync_state", "created_at")
    list_filter = ("terms", "qbo_sync_state")
    search_fields = ("name", "contact__name", "qbo_id")
    autocomplete_fields = ("contact",)
    inlines = [AccountGroupInline]

    def save_model(self, request, obj, form, change):
        requeue = change and _requeue_if_changed(obj, set(form.changed_data))
        super().save_model(request, obj, form, change)
        if requeue:  # groups may inherit the account's terms
            obj.groups.update(qbo_sync_state=SyncState.NOT_SYNCED)

    def save_formset(self, request, form, formset, change):
        for inline_form in formset.forms:
            if inline_form.instance.pk and inline_form.has_changed():
                _requeue_if_changed(inline_form.instance, set(inline_form.changed_data))
        super().save_formset(request, form, formset, change)


@admin.register(AccountGroup)
class AccountGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "account", "invoice_email", "is_default", "qbo_sync_state")
    list_filter = ("is_default", "po_required", "qbo_sync_state")
    search_fields = ("name", "account__name", "invoice_email")

    def save_model(self, request, obj, form, change):
        if change:
            _requeue_if_changed(obj, set(form.changed_data))
        super().save_model(request, obj, form, change)


@admin.register(QboTerm)
class QboTermAdmin(admin.ModelAdmin):
    """Read from QuickBooks by the hourly sync and Settings → QuickBooks → Refresh terms."""

    list_display = ("name", "qbo_id", "active", "updated_at")
    list_filter = ("active",)
    readonly_fields = ("qbo_id", "name", "active")

    def has_add_permission(self, request):
        return False


@admin.register(QboAccount)
class QboAccountAdmin(admin.ModelAdmin):
    """Read from QuickBooks by the hourly sync and Settings → QuickBooks → Refresh accounts."""

    list_display = ("name", "account_type", "qbo_id", "realm_id", "active", "updated_at")
    list_filter = ("active", "account_type")
    readonly_fields = ("realm_id", "qbo_id", "name", "account_type", "active")

    def has_add_permission(self, request):
        return False


@admin.register(QboItem)
class QboItemAdmin(admin.ModelAdmin):
    """Read from QuickBooks by the hourly sync and Settings → QuickBooks."""

    list_display = ("name", "item_type", "realm_id", "active")
    list_filter = ("active", "item_type")
    readonly_fields = ("realm_id", "qbo_id", "name", "item_type", "active")

    def has_add_permission(self, request):
        return False


@admin.register(QboAccountMapping)
class QboAccountMappingAdmin(admin.ModelAdmin):
    """Chosen on Settings → QuickBooks; shown here for support."""

    list_display = (
        "__str__",
        "expense_account",
        "payment_account",
        "bills_from",
        "trip_item",
        "customer_docs_from",
    )

    def has_add_permission(self, request):
        return False


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    fields = ("reservation", "description", "amount")
    readonly_fields = ("reservation", "description", "amount")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("pk", "number", "group", "invoice_date", "due_date", "total", "status")
    list_filter = ("status",)
    search_fields = ("number", "group__name", "group__account__name")
    list_select_related = ("group__account",)
    inlines = [InvoiceLineInline]

    def get_readonly_fields(self, request, obj=None):
        """Lines and the total are frozen at creation; status changes go through void."""
        if obj is None:
            return self.readonly_fields
        return (*self.readonly_fields, "status", "total", "group", "number")
