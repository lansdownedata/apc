from django.contrib import admin

from .models import Assignment, GnetEvent


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "reservation",
        "vendor",
        "driver",
        "vehicle",
        "status",
        "channel",
        "payout",
        "offered_at",
    )
    list_filter = ("status", "channel")
    search_fields = ("vendor__name", "driver__name", "reservation__lead__contact__name")
    autocomplete_fields = ("reservation", "vendor", "driver", "vehicle")


@admin.register(GnetEvent)
class GnetEventAdmin(admin.ModelAdmin):
    list_display = ("action", "result", "idempotency_key", "created_at")
    list_filter = ("action", "result")
    search_fields = ("idempotency_key",)
    readonly_fields = ("payload", "response")
