from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Owner/admin and agent accounts for the Lead Manager.

    Custom user defined up front so AUTH_USER_MODEL is stable before the first
    migration (swapping it later is painful).
    """

    class Role(models.TextChoices):
        OWNER_ADMIN = "owner_admin", "Admin"
        AGENT = "agent", "Agent"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        DEACTIVATED = "deactivated", "Deactivated"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(max_length=32, blank=True)
    two_factor_enabled = models.BooleanField(default=False)
    can_manage_payments = models.BooleanField(
        "can manage payments",
        default=False,
        help_text="May run money actions (refunds, mark-paid, retry charges).",
    )
    invited_at = models.DateTimeField(null=True, blank=True)
    invite_accepted_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invitees",
    )

    @property
    def status(self) -> str:
        """Derived, never stored — a stored status drifts out of sync with is_active.

        is_active is checked first: a deactivated account is deactivated whatever its
        invite state. A user with no invited_at predates invites and reads as Active.
        """
        if not self.is_active:
            return self.Status.DEACTIVATED
        if self.invited_at and not self.invite_accepted_at:
            return self.Status.PENDING
        return self.Status.ACTIVE

    @property
    def has_payments_access(self) -> bool:
        return self.is_owner_admin or self.can_manage_payments

    @property
    def is_owner_admin(self) -> bool:
        return self.role == self.Role.OWNER_ADMIN

    def __str__(self) -> str:
        return self.get_full_name() or self.username
