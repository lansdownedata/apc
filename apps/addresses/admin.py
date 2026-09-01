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


class HasVehicleCapFilter(admin.SimpleListFilter):
    """ "Do we have a vehicle-access limit on file?" — lets the office work the gaps
    left by the client's venue -> limit list (APC-9)."""

    title = "vehicle cap on file"
    parameter_name = "has_cap"

    def lookups(self, request, model_admin):
        return (("yes", "Cap on file"), ("no", "No cap set"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(vehicle_cap__isnull=False)
        if self.value() == "no":
            return queryset.filter(vehicle_cap__isnull=True)
        return queryset


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "kind",
        "city",
        "state",
        "vehicle_cap",
        "has_cap",
        "lead_hits",
        "is_active",
    )
    list_editable = ("vehicle_cap",)
    list_filter = (HasVehicleCapFilter, "kind", "is_active", "state")
    search_fields = ("name", "city", "cap_note", "access_note")
    # A second key keeps changelist pagination stable while `list_editable` is on —
    # `name` alone is not unique across the directory.
    ordering = ("name", "pk")
    list_per_page = 50
    fieldsets = (
        (None, {"fields": ("name", "kind", "is_active")}),
        (
            "Location",
            {
                "fields": (
                    "address",
                    "city",
                    "state",
                    "latitude",
                    "longitude",
                    "locationiq_place_id",
                )
            },
        ),
        (
            "Vehicle access",
            {
                "fields": ("vehicle_cap", "cap_note", "access_note"),
                "description": (
                    "Largest vehicle this venue allows and why (gravel drive, low bridge, "
                    "short turning circle). Shown on the wedding recommendation page and used "
                    "to gate the vehicle suggestion — leave blank if unknown."
                ),
            },
        ),
        ("Directory ranking", {"fields": ("lead_hits",)}),
    )

    @admin.display(description="Cap?", boolean=True, ordering="vehicle_cap")
    def has_cap(self, obj) -> bool:
        return obj.vehicle_cap is not None
