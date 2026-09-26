from django.contrib import admin

from .models import TaskConfig


@admin.register(TaskConfig)
class TaskConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "overdue_grace_hours")
