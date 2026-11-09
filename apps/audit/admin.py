"""The trail in Django admin (spec §8): auditlog's read-only LogEntryAdmin, which it
registers for the swapped AuditEntry, plus where each entry came from."""

from auditlog.admin import LogEntryAdmin
from django.contrib import admin

from .models import AuditEntry


class AuditEntryAdmin(LogEntryAdmin):
    """Add, change and delete stay forbidden: inherited from LogEntryAdmin.

    No date hierarchy: it truncates `timestamp` in the database's time zone, which MySQL
    (local and test) can't do without its zone tables loaded. A date filter does the same
    job with plain range lookups.
    """

    date_hierarchy = None
    list_display = [*LogEntryAdmin.list_display, "source", "actor_email"]
    list_filter = [
        *LogEntryAdmin.list_filter,
        ("timestamp", admin.DateFieldListFilter),
        "source",
        "actor_email",
    ]


admin.site.unregister(AuditEntry)
admin.site.register(AuditEntry, AuditEntryAdmin)
