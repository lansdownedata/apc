from django.contrib import admin

from .models import ImportRun


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "screen", "user", "filename", "status")
    list_filter = ("kind", "screen", "status")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
