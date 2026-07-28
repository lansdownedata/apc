from django.contrib import admin

from .models import Address, Airport


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("__str__", "city", "state", "postal")
    search_fields = ("landmark_name", "line1", "city", "postal")


@admin.register(Airport)
class AirportAdmin(admin.ModelAdmin):
    list_display = ("iata", "name", "city", "state", "size", "is_active", "enriched_at")
    list_filter = ("size", "is_active", "state")
    search_fields = ("name", "city", "iata", "icao", "ident")
    ordering = ("name",)
