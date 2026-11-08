from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class ImportRun(TimeStampedModel):
    """One workbook import or export on the Contacts / Vendors screens."""

    class Kind(models.TextChoices):
        IMPORT = "import", "Import"
        EXPORT = "export", "Export"

    class Screen(models.TextChoices):
        CONTACTS = "contacts", "Contacts"
        VENDORS = "vendors", "Vendors"

    class Status(models.TextChoices):
        PREVIEWED = "previewed", "Preview"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    screen = models.CharField(max_length=10, choices=Screen.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    filename = models.CharField(max_length=255)
    file = models.FileField(upload_to="imports/%Y/%m/", blank=True)
    status = models.CharField(max_length=10, choices=Status.choices)
    filters = models.JSONField(default=dict, blank=True)
    preview_counts = models.JSONField(default=dict, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    rows = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["screen", "kind", "-created_at"], name="imports_screen_kind_idx")
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.get_screen_display()} {self.filename}"

    @property
    def is_expired(self) -> bool:
        """A preview is confirmable for PREVIEW_TTL; applied/failed runs never expire."""
        from .services import PREVIEW_TTL

        cutoff = timezone.now() - PREVIEW_TTL
        return self.status == self.Status.PREVIEWED and self.created_at < cutoff
