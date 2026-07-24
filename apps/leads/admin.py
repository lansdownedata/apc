from django.contrib import admin

from .models import Lead, VehicleType


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "capacity", "sort_order", "active")
    list_editable = ("sort_order", "active")


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "quote_no",
        "contact",
        "status",
        "channel",
        "assigned_agent",
        "has_alert",
        "created_at",
    )
    list_filter = ("status", "channel", "has_alert")
    search_fields = ("contact__name", "contact__company__name", "notes")
    autocomplete_fields = ("contact", "assigned_agent")
    list_select_related = ("contact", "assigned_agent")
