from django.contrib import admin

from .models import LACustomer, LAEvent, PodiumCredential, PodiumEvent, ZapEvent


@admin.register(PodiumCredential)
class PodiumCredentialAdmin(admin.ModelAdmin):
    list_display = ("__str__", "organization_uid", "location_uid", "expires_at", "updated_at")
    readonly_fields = ("access_token", "refresh_token", "created_at", "updated_at")


@admin.register(ZapEvent)
class ZapEventAdmin(admin.ModelAdmin):
    list_display = ("action", "lead", "result", "idempotency_key", "created_at")
    list_filter = ("action", "result")
    search_fields = ("idempotency_key",)


@admin.register(PodiumEvent)
class PodiumEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "lead", "processed", "created_at")
    list_filter = ("event_type", "processed")


@admin.register(LACustomer)
class LACustomerAdmin(admin.ModelAdmin):
    list_display = ("__str__", "contact", "la_customer_id", "created_at")
    readonly_fields = ("password_encrypted", "created_at", "updated_at")


@admin.register(LAEvent)
class LAEventAdmin(admin.ModelAdmin):
    list_display = ("__str__", "event", "la_customer", "reservation", "created_at")
    list_filter = ("event",)
