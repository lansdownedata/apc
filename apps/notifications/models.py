from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(read=False)


class Notification(TimeStampedModel):
    """In-app alert that drives the bell (e.g. a failed balance charge)."""

    class Kind(models.TextChoices):
        BALANCE_FAILED = "balance_failed", "Balance charge failed"
        BALANCE_PAID = "balance_paid", "Balance paid"
        DEPOSIT_PAID = "deposit_paid", "Deposit received"
        SYNC_FAILED = "sync_failed", "Sync failed"
        NEW_LEAD = "new_lead", "New lead"

    lead = models.ForeignKey("leads.Lead", related_name="notifications", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="notifications",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=160)
    detail = models.CharField(max_length=255, blank=True)
    read = models.BooleanField(default=False)

    objects = NotificationQuerySet.as_manager()

    @classmethod
    def notify(cls, lead, kind, *, title, detail="", user=None) -> "Notification":
        return cls.objects.create(lead=lead, kind=kind, title=title, detail=detail, user=user)

    def mark_read(self) -> None:
        if not self.read:
            self.read = True
            self.save(update_fields=["read", "updated_at"])

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.title}"
