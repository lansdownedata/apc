from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base — created/updated timestamps for every domain model."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class Throttle(models.Model):
    """One counter of the sign-in, sign-up and reset brakes (`apps.core.throttle`):
    how many hits one IP address or email address has made in the window that ends at
    `expires_at`. Housekeeping, not history: rows are swept once their window runs out."""

    # SHA-256 of the scope and who is counted; the address itself is never stored.
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return f"{self.key[:12]} · {self.count}"
