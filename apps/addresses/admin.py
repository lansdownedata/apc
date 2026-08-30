from django.contrib import admin

from .models import Address, Airline, Airport, Venue


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


@admin.register(Airline)
class AirlineAdmin(admin.ModelAdmin):
    list_display = ("iata", "name", "icao", "is_active")
    list_filter = ("is_active",)
    search_fields = ("iata", "icao", "name")
    ordering = ("name",)


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "city", "state", "vehicle_cap", "lead_hits", "is_active")
    list_filter = ("kind", "is_active", "state")
    search_fields = ("name", "city")
    ordering = ("name",)
