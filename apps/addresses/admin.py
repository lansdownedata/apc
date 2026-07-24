from django.contrib import admin

from .models import Address


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("__str__", "city", "state", "postal")
    search_fields = ("landmark_name", "line1", "city", "postal")
