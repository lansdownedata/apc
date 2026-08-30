from django.contrib import admin

from .models import Driver


@admin.register(Driver)
class DriverAdmin(admin.ModelAdmin):
    list_display = ("driver_number", "name", "phone", "email", "status")
    list_filter = ("status",)
    search_fields = ("name", "phone", "email", "driver_number")
    readonly_fields = ("driver_number",)
