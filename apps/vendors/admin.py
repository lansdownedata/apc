from django.contrib import admin

from .models import Vendor


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "service_area", "contact_name", "phone", "email")
    list_filter = ("status",)
    search_fields = ("name", "contact_name", "email", "phone", "usdot_number")
    filter_horizontal = ("vehicle_types",)
