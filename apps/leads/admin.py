from django.contrib import admin

from .models import Lead, Vehicle


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("name", "klass", "capacity", "active")
    list_filter = ("klass", "active")
    search_fields = ("name",)


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
    search_fields = ("contact__name", "contact__company", "notes")
    autocomplete_fields = ("contact", "assigned_agent")
    list_select_related = ("contact", "assigned_agent")
