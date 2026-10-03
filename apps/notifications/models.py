from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class NotificationQuerySet(models.QuerySet):
    def unread(self):
        return self.filter(read=False)

    def for_user(self, user):
        """What `user`'s tray is allowed to show (APC-77): notifications addressed to them,
        plus the shared ones (`user` null — every `Notification.notify` caller that names
        no one: payment, dispatch, sync and lead alerts), which all staff see."""
        return self.filter(models.Q(user=user) | models.Q(user__isnull=True))

    def unread_for(self, user):
        """`for_user`, minus what they've read. A shared alert is read per person (a
        `NotificationRead` receipt), so one reader doesn't hide it from the rest."""
        receipt = NotificationRead.objects.filter(notification=models.OuterRef("pk"), user=user)
        return self.for_user(user).filter(read=False).exclude(models.Exists(receipt))


class Notification(TimeStampedModel):
    """In-app alert that drives the bell (e.g. a failed balance charge)."""

    class Kind(models.TextChoices):
        BALANCE_FAILED = "balance_failed", "Balance charge failed"
        BALANCE_PAID = "balance_paid", "Balance paid"
        DEPOSIT_PAID = "deposit_paid", "Deposit received"
        SYNC_FAILED = "sync_failed", "Sync failed"
        NEW_LEAD = "new_lead", "New lead"
        LA_CHANGED = "la_changed", "Update LimoAnywhere"
        QUOTE_EXPIRED = "quote_expired", "Quote expired"
        DISPATCH_EXCEPTION = "dispatch_exception", "Dispatch exception"
        # A held deposit nearing / past the issuer's release window (APC-26).
        AUTH_EXPIRING = "auth_expiring", "Deposit hold expiring"
        AUTH_EXPIRED = "auth_expired", "Deposit hold released"
        TASK_OVERDUE = "task_overdue", "Task overdue"

    # Every notification is about an order, except a vendor task's escalation (APC-71).
    lead = models.ForeignKey(
        "leads.Lead",
        related_name="notifications",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    vendor = models.ForeignKey(
        "vendors.Vendor",
        related_name="notifications",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
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
    read_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    @classmethod
    def notify(cls, lead, kind, *, title, detail="", user=None) -> "Notification":
        return cls.objects.create(lead=lead, kind=kind, title=title, detail=detail, user=user)

    @property
    def url(self) -> str:
        """Where the tray and dashboard send you: the order, or the vendor."""
        from django.urls import reverse

        if self.lead_id:
            return reverse("lead_detail", args=[self.lead_id])
        if self.vendor_id:
            return reverse("vendor_detail", args=[self.vendor_id])
        return ""

    def mark_read(self) -> None:
        if not self.read:
            self.read = True
            self.read_at = timezone.now()
            self.save(update_fields=["read", "read_at", "updated_at"])

    def mark_read_by(self, user) -> None:
        """`user` has seen it: their own notification is read outright, a shared one gets
        their receipt."""
        if self.user_id is None:
            NotificationRead.objects.get_or_create(notification=self, user=user)
        else:
            self.mark_read()

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.title}"


class NotificationRead(models.Model):
    """One person's "seen it" on a shared notification (APC-77)."""

    notification = models.ForeignKey(Notification, related_name="reads", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="notification_reads", on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["notification", "user"], name="uniq_notification_read")
        ]

    def __str__(self) -> str:
        return f"{self.user_id} read {self.notification_id}"


def mark_all_read(user) -> None:
    """Clear `user`'s tray: their own notifications are read, shared ones get a receipt."""
    unread = Notification.objects.unread_for(user)
    shared = list(unread.filter(user__isnull=True).values_list("pk", flat=True))
    unread.filter(user=user).update(read=True, read_at=timezone.now(), updated_at=timezone.now())
    NotificationRead.objects.bulk_create(
        [NotificationRead(notification_id=pk, user=user) for pk in shared], ignore_conflicts=True
    )
