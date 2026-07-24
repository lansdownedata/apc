from django.contrib import admin

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
