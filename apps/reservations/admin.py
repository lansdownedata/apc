from django.contrib import admin

from .models import Flight, Reservation, Stop, TripStatusEvent


class StopInline(admin.TabularInline):
    model = Stop
    extra = 0


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = ("__str__", "lead", "trip_type", "trip_status", "pickup_date", "line_total")
    list_filter = ("trip_type", "trip_status")
    search_fields = ("service", "lead__contact__name")
    inlines = [StopInline]
    list_select_related = ("lead",)


@admin.register(TripStatusEvent)
class TripStatusEventAdmin(admin.ModelAdmin):
    list_display = ("reservation", "status", "source", "changed_by", "created_at")
    list_filter = ("status", "source")


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    list_display = ("__str__", "airport", "direction", "flight_date", "status", "checked_at")
    list_filter = ("status", "source", "direction")
    search_fields = ("flight_number", "airline__iata", "airport__iata")
    list_select_related = ("airline", "airport")
    readonly_fields = ("raw",)
