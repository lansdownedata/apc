from django.contrib import admin

from .models import Task, TaskConfig


@admin.register(TaskConfig)
class TaskConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "overdue_grace_hours")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("kind", "lead", "reservation", "department", "status", "due_at", "assignee")
    list_filter = ("status", "department", "kind")
    list_select_related = ("lead__contact", "reservation__service_type", "assignee")
    raw_id_fields = ("lead", "reservation", "assignee", "completed_by")
    search_fields = ("kind", "note")
