from django.contrib import admin

from .models import Driver, Renewal, RenewalType, Vehicle


class RenewalInline(admin.TabularInline):
    model = Renewal
    extra = 0
    fields = ("renewal_type", "reference", "issued_on", "expires_on", "document")


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("driver_number", "name", "phone", "email", "status")
    list_filter = ("status",)
    search_fields = ("name", "phone", "email", "driver_number")
    readonly_fields = ("driver_number",)
    inlines = (RenewalInline,)


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("name", "vehicle_type", "license_plate", "status")
    list_filter = ("status", "vehicle_type")
    search_fields = ("name", "license_plate", "vin", "make", "model_name")
    inlines = (RenewalInline,)


@admin.register(RenewalType)
class RenewalTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "applies_to", "active", "sort_order")
    list_filter = ("applies_to", "active")
