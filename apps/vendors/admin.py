from django.contrib import admin

from apps.billing.models import SyncState

from .models import Vendor, VendorDocument, VendorDriver, VendorInsurance


class VendorDriverInline(admin.TabularInline):
    model = VendorDriver
    extra = 0


class VendorDocumentInline(admin.TabularInline):
    model = VendorDocument
    extra = 0
    readonly_fields = ("uploaded_by",)


class VendorInsuranceInline(admin.TabularInline):
    model = VendorInsurance
    extra = 0


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "service_area", "contact_name", "phone", "email")
    list_filter = ("status",)
    search_fields = ("name", "contact_name", "email", "phone", "usdot_number")
    filter_horizontal = ("vehicle_types",)
    inlines = (VendorDriverInline, VendorDocumentInline, VendorInsuranceInline)
    readonly_fields = ("qbo_id", "qbo_sync_token", "qbo_realm_id", "qbo_synced_at")

    def save_model(self, request, obj, form, change):
        if change and set(form.changed_data) & set(Vendor.QBO_FIELDS):
            obj.qbo_sync_state = SyncState.NOT_SYNCED  # its QuickBooks vendor needs it (APC-74)
        super().save_model(request, obj, form, change)
