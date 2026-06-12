from django.contrib import admin

from .models import Message, Review, TouchPoint


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("lead", "direction", "channel", "delivery_status", "sent_at", "created_at")
    list_filter = ("direction", "channel", "delivery_status")
    search_fields = ("body", "lead__contact__name")


@admin.register(TouchPoint)
class TouchPointAdmin(admin.ModelAdmin):
    list_display = ("lead", "kind", "status", "scheduled_for", "sent_at")
    list_filter = ("kind", "status")


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("lead", "contact", "delivery_status", "link_clicked", "rating", "requested_at")
    list_filter = ("delivery_status", "link_clicked")
