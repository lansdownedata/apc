from django.contrib import admin

from .models import Assignment, FarmInRequest, FarmInStatusPush, GnetEvent


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


@admin.register(FarmInRequest)
class FarmInRequestAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "company", "desk", "status", "pickup_at")
    list_filter = ("status", "desk")
    search_fields = ("transaction_id", "requester_grid_id", "requester_res_no")
    readonly_fields = ("payload",)


@admin.register(FarmInStatusPush)
class FarmInStatusPushAdmin(admin.ModelAdmin):
    list_display = ("request", "status", "state", "attempts", "next_attempt_at")
    list_filter = ("state",)
