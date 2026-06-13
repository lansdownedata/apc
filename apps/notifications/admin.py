from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "lead", "title", "read", "created_at")
    list_filter = ("kind", "read")
    search_fields = ("title", "detail", "lead__contact__name")
